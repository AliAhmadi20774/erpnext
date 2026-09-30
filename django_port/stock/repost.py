"""Atomic cancellation and chronological valuation replay for supported Stock Entries."""

from collections import defaultdict
from copy import copy
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounting.ledger import LedgerLine, post_gl_entries
from accounting.models import GLEntry, PeriodClosingVoucher
from accounting.periods import validate_accounting_period
from organizations.models import Company

from .entries import _gl_lines, _stock_value_totals
from .ledger import _decimal, _normalise_queue, _queue_valuation, _serialise_queue
from .models import (
    Bin, ReceiptRateCorrection, StockEntry, StockEntryDetail, StockLedgerEntry,
    StockEntryType, StockReconciliation,
)


ZERO = Decimal("0")
VALUATION_FIELDS = (
    "qty_after_transaction", "incoming_rate", "outgoing_rate", "valuation_rate",
    "stock_value", "stock_value_difference", "stock_queue",
)


def _reconciliation_source_names(company):
    """Resolve native reconciliation vouchers to their internal receipt entries."""
    return dict(StockReconciliation.objects.filter(
        company=company, receipt_entry__isnull=False,
    ).values_list("pk", "receipt_entry_id"))


def _entry_name(sle, source_names):
    if sle.voucher_type == "Stock Reconciliation":
        return source_names.get(sle.voucher_no, sle.voucher_no)
    return sle.voucher_no


def _voucher_identities(ledger, source_names):
    identities = {}
    for sle in ledger:
        name = _entry_name(sle, source_names)
        if sle.voucher_type == "Stock Reconciliation" and sle.voucher_no != name:
            identities[name] = ("Stock Reconciliation", sle.voucher_no)
        else:
            identities.setdefault(name, ("Stock Entry", name))
    return identities


def _check_open_period(company, entry, user):
    validate_accounting_period(
        company=company, posting_date=entry.posting_date,
        document_type="Stock Entry", user=user,
    )
    if PeriodClosingVoucher.objects.filter(
        company=company, status=PeriodClosingVoucher.Status.SUBMITTED,
        period_end_date__gte=entry.posting_date,
    ).exists():
        raise ValidationError("Cannot cancel or repost stock in a closed accounting period.")


def _gl_difference(old_lines, new_lines, voucher_type, voucher_name):
    totals = defaultdict(lambda: ZERO)
    accounts = {}
    dimensions = {}
    for sign, lines in ((-1, old_lines), (1, new_lines)):
        for line in lines:
            key = (
                line.account.pk,
                line.cost_center.pk if line.cost_center else None,
                line.project.pk if line.project else None,
                line.finance_book.pk if line.finance_book else None,
            )
            totals[key] += sign * (line.debit - line.credit)
            accounts[key] = line.account
            dimensions[key] = (line.cost_center, line.project, line.finance_book)
    result = []
    for key, amount in totals.items():
        if amount:
            center, project, book = dimensions[key]
            result.append(LedgerLine(
                account=accounts[key], debit=max(amount, ZERO),
                credit=max(-amount, ZERO), cost_center=center,
                project=project, finance_book=book,
                remarks="Stock Entry valuation replay",
                against_voucher_type=voucher_type, against_voucher=voucher_name,
            ))
    return result


def _signed_gl_totals(lines):
    totals = defaultdict(lambda: ZERO)
    for line in lines:
        account_id = line.account_id if isinstance(line, GLEntry) else line.account.pk
        center_id = line.cost_center_id if isinstance(line, GLEntry) else (line.cost_center.pk if line.cost_center else None)
        project_id = line.project_id if isinstance(line, GLEntry) else (line.project.pk if line.project else None)
        book_id = line.finance_book_id if isinstance(line, GLEntry) else (line.finance_book.pk if line.finance_book else None)
        totals[(account_id, center_id, project_id, book_id)] += line.debit - line.credit
    return {key: amount for key, amount in totals.items() if amount}


def _update_entry_totals(entry, rows, by_detail):
    total = ZERO
    for row in rows:
        outgoing = by_detail.get(f"{row.pk}:OUT")
        incoming = by_detail.get(f"{row.pk}:IN")
        if outgoing:
            row.basic_rate = outgoing.outgoing_rate
            row.basic_amount = -outgoing.stock_value_difference
            row.amount = row.basic_amount
            row.actual_qty = outgoing.qty_after_transaction
            row.valuation_rate = outgoing.valuation_rate
        if incoming:
            if not outgoing:
                row.basic_rate = incoming.incoming_rate
                row.basic_amount = incoming.stock_value_difference
                row.amount = row.basic_amount
            row.actual_qty = incoming.qty_after_transaction
            row.valuation_rate = incoming.valuation_rate
        row.save(_allow_repost=True, update_fields=(
            "basic_rate", "basic_amount", "amount", "actual_qty", "valuation_rate",
        ))
        total += row.amount
    entry.total_incoming_value, entry.total_outgoing_value = _stock_value_totals(
        by_detail.values()
    )
    entry.value_difference = entry.total_incoming_value - entry.total_outgoing_value
    entry.total_amount = total
    entry.save(_allow_repost=True, update_fields=(
        "total_incoming_value", "total_outgoing_value", "value_difference", "total_amount",
    ))


def _check_reconciliation_counts(company, ledger, source_names):
    active = defaultdict(list)
    for sle in ledger:
        if not sle.is_cancelled and sle.voucher_type in {"Stock Entry", "Stock Reconciliation"}:
            active[(_entry_name(sle, source_names), sle.item_id, sle.warehouse_id)].append(sle)
    for reconciliation in StockReconciliation.objects.filter(
        company=company, status=StockReconciliation.Status.SUBMITTED
    ).prefetch_related("items"):
        for row in reconciliation.items.all():
            if row.revalue_existing_stock:
                if row.direct_value_adjustment:
                    direct = active.get((
                        reconciliation.receipt_entry_id, row.item_id, row.warehouse_id
                    ), [])
                    if (len(direct) != 1 or not direct[0].is_value_reset
                            or direct[0].actual_qty != ZERO
                            or direct[0].qty_after_transaction != row.counted_qty):
                        raise ValidationError(
                            "Replay would invalidate a submitted direct stock value reset; cancel its Stock Reconciliation first."
                        )
                    continue
                outgoing = active.get((
                    reconciliation.issue_entry_id, row.item_id, row.warehouse_id
                ), [])
                incoming = active.get((
                    reconciliation.receipt_entry_id, row.item_id, row.warehouse_id
                ), [])
                if (len(outgoing) != 1 or len(incoming) != 1
                        or outgoing[0].qty_after_transaction != ZERO
                        or incoming[0].qty_after_transaction != row.counted_qty):
                    raise ValidationError(
                        "Replay would invalidate a submitted stock value reset; cancel its Stock Reconciliation first."
                    )
                continue
            if not row.difference_qty:
                continue
            entry_id = (
                reconciliation.receipt_entry_id if row.difference_qty > ZERO
                else reconciliation.issue_entry_id
            )
            matches = active.get((entry_id, row.item_id, row.warehouse_id), [])
            if len(matches) != 1 or matches[0].qty_after_transaction != row.counted_qty:
                raise ValidationError(
                    "Replay would invalidate a submitted stock count; cancel its Stock Reconciliation first."
                )


def _revalue_ledger(company, ledger, source_names, *, new_vouchers=frozenset()):
    """Replay active rows in posting order; return changed old vouchers and final balances."""
    affected = set()
    states = {}
    outgoing = {}
    for sle in ledger:
        if sle.is_cancelled:
            continue
        key = (sle.item_id, sle.warehouse_id)
        old_qty, old_value, old_rate, queue = states.get(key, (ZERO, ZERO, ZERO, []))
        quantity = _decimal(sle.actual_qty)
        new_qty = _decimal(old_qty + quantity)
        if new_qty < ZERO:
            raise ValidationError("Stock replay would create negative stock in a later voucher.")
        source = None
        incoming_layers = None
        incoming_rate = sle.incoming_rate if quantity > ZERO or sle.is_value_reset else ZERO
        if sle.dependant_sle_voucher_detail_no:
            source = outgoing.get((sle.voucher_no, sle.dependant_sle_voucher_detail_no))
            if source is None or quantity <= ZERO or -source[0].actual_qty != quantity or source[0].item_id != sle.item_id:
                raise ValidationError("A transfer source is missing during stock replay.")
            incoming_rate = source[0].outgoing_rate
            incoming_layers = source[1]
        consumed_layers = []
        if sle.is_value_reset:
            if quantity != ZERO or old_qty <= ZERO or sle.voucher_type not in {"Stock Entry", "Stock Reconciliation"}:
                raise ValidationError("A direct value adjustment requires unchanged positive stock.")
            outgoing_rate = ZERO
            new_value = _decimal(old_qty * incoming_rate)
            new_queue = [] if company.valuation_method == Company.ValuationMethod.MOVING_AVERAGE else [[old_qty, incoming_rate]]
        elif company.valuation_method == Company.ValuationMethod.MOVING_AVERAGE:
            if quantity > ZERO:
                new_value = _decimal(old_value + quantity * incoming_rate)
                outgoing_rate = ZERO
            else:
                outgoing_rate = _decimal(old_rate)
                new_value = _decimal(old_value + quantity * outgoing_rate)
            if new_qty == ZERO:
                new_value = ZERO
            new_queue = []
        else:
            new_queue, new_value, outgoing_rate, consumed_layers = _queue_valuation(
                queue=_normalise_queue(queue), quantity=quantity,
                incoming_rate=incoming_rate,
                lifo=company.valuation_method == Company.ValuationMethod.LIFO,
                incoming_layers=incoming_layers,
            )
        difference = _decimal(new_value - old_value)
        if source and difference != -source[0].stock_value_difference:
            raise ValidationError("Transfer value no longer balances during stock replay.")
        values = {
            "qty_after_transaction": new_qty,
            "incoming_rate": incoming_rate,
            "outgoing_rate": outgoing_rate,
            "valuation_rate": _decimal(new_value / new_qty) if new_qty else ZERO,
            "stock_value": new_value,
            "stock_value_difference": difference,
            "stock_queue": _serialise_queue(new_queue),
        }
        if any(getattr(sle, field) != value for field, value in values.items()):
            for field, value in values.items():
                setattr(sle, field, value)
            sle.save(_allow_repost=True, update_fields=VALUATION_FIELDS)
            name = _entry_name(sle, source_names)
            if name not in new_vouchers:
                affected.add(name)
        states[key] = (new_qty, new_value, values["valuation_rate"], new_queue)
        if quantity < ZERO:
            outgoing[(sle.voucher_no, sle.voucher_detail_no)] = (sle, consumed_layers)
    _check_reconciliation_counts(company, ledger, source_names)
    return affected, states


def _rebuild_bins(bins, states):
    for key, item_bin in bins.items():
        quantity, value, rate, _ = states.get(key, (ZERO, ZERO, ZERO, []))
        if (item_bin.actual_qty, item_bin.stock_value, item_bin.valuation_rate) != (quantity, value, rate):
            item_bin.actual_qty = quantity
            item_bin.stock_value = value
            item_bin.valuation_rate = rate
            item_bin.save(_allow_stock_write=True)


def _repost_affected(company, ledger, original, documents, affected, user, source_names):
    active = defaultdict(dict)
    old_active = defaultdict(dict)
    identities = _voucher_identities(ledger, source_names)
    for sle in ledger:
        if not sle.is_cancelled:
            name = _entry_name(sle, source_names)
            active[name][sle.voucher_detail_no] = sle
            old_active[name][sle.voucher_detail_no] = original[sle.pk]
    for name in affected:
        entry = documents[name]
        voucher_type, voucher_no = identities[name]
        _check_open_period(company, entry, user)
        rows = list(entry.items.select_related(
            "source_warehouse", "target_warehouse", "expense_account", "cost_center", "project"
        ).order_by("position", "id"))
        if entry.perpetual_inventory_at_submit:
            existing_gl = list(GLEntry.objects.filter(
                company=company, voucher_type=voucher_type, voucher_no=voucher_no,
            )) + list(GLEntry.objects.filter(
                company=company, voucher_type="Stock Valuation Repost",
                against_voucher_type=voucher_type, against_voucher=voucher_no,
            ))
            original_lines = _gl_lines(entry, company, rows, old_active[name])
            if _signed_gl_totals(existing_gl) != _signed_gl_totals(original_lines):
                raise ValidationError(
                    "Existing GL does not match this Stock Entry valuation; historical accounting dimensions need reconciliation."
                )
            delta = _gl_difference(
                original_lines, _gl_lines(entry, company, rows, active[name]),
                voucher_type, voucher_no,
            )
            if delta:
                post_gl_entries(
                    company=company, posting_date=entry.posting_date,
                    voucher_type="Stock Valuation Repost",
                    voucher_no=f"STOCK-RPV-{uuid4().hex}",
                    lines=delta, is_opening=entry.is_opening, user=user,
                )
        elif GLEntry.objects.filter(
            company=company, voucher_type=voucher_type, voucher_no=voucher_no
        ).exists():
            raise ValidationError("The historical Stock Entry perpetual-inventory setting is missing.")
        _update_entry_totals(entry, rows, active[name])
    return active


def _refresh_reconciliation_values(company, ledger, source_names):
    """Keep submitted count snapshots aligned with a successful historical replay."""
    active = defaultdict(list)
    for sle in ledger:
        if not sle.is_cancelled and sle.voucher_type in {"Stock Entry", "Stock Reconciliation"}:
            active[(_entry_name(sle, source_names), sle.item_id, sle.warehouse_id)].append(sle)
    for reconciliation in StockReconciliation.objects.filter(
        company=company, status=StockReconciliation.Status.SUBMITTED
    ).prefetch_related("items"):
        total = ZERO
        for row in reconciliation.items.all():
            if row.revalue_existing_stock:
                if row.direct_value_adjustment:
                    direct = active[(
                        reconciliation.receipt_entry_id, row.item_id, row.warehouse_id
                    )][0]
                    previous_value = direct.stock_value - direct.stock_value_difference
                    value_difference = direct.stock_value_difference
                else:
                    outgoing = active[(
                        reconciliation.issue_entry_id, row.item_id, row.warehouse_id
                    )][0]
                    incoming = active[(
                        reconciliation.receipt_entry_id, row.item_id, row.warehouse_id
                    )][0]
                    previous_value = -outgoing.stock_value_difference
                    value_difference = outgoing.stock_value_difference + incoming.stock_value_difference
            elif row.difference_qty:
                entry_id = (
                    reconciliation.receipt_entry_id if row.difference_qty > ZERO
                    else reconciliation.issue_entry_id
                )
                sle = active[(entry_id, row.item_id, row.warehouse_id)][0]
                previous_value = sle.stock_value - sle.stock_value_difference
                value_difference = sle.stock_value_difference
            else:
                total += row.value_difference or ZERO
                continue
            previous_rate = _decimal(previous_value / row.previous_qty) if row.previous_qty else ZERO
            if (row.previous_stock_value, row.previous_valuation_rate, row.value_difference) != (
                previous_value, previous_rate, value_difference
            ):
                row.previous_stock_value = previous_value
                row.previous_valuation_rate = previous_rate
                row.value_difference = value_difference
                row.save(_submitting=True, update_fields=(
                    "previous_stock_value", "previous_valuation_rate", "value_difference",
                ))
            total += value_difference
        if reconciliation.total_value_difference != total:
            reconciliation.total_value_difference = total
            reconciliation.save(_lifecycle=True, update_fields=("total_value_difference",))


@transaction.atomic
def replay_new_stock_entries(stock_entries, *, user=None):
    """Value a group of staged vouchers before replaying affected future stock."""
    stock_entries = tuple(stock_entries)
    if not stock_entries or any(
        not isinstance(entry, StockEntry) or not entry.pk for entry in stock_entries
    ):
        raise TypeError("stock_entries must contain saved Stock Entries")
    new_names = {entry.pk for entry in stock_entries}
    if (
        len(new_names) != len(stock_entries)
        or len({entry.company_id for entry in stock_entries}) != 1
        or len({(entry.posting_date, entry.posting_time) for entry in stock_entries}) != 1
    ):
        raise ValidationError("Staged Stock Entries must be distinct and share a company and posting time.")
    company = Company.objects.select_for_update().get(pk=stock_entries[0].company_id)
    ledger = list(StockLedgerEntry.objects.select_for_update().filter(
        company=company
    ).order_by("posting_datetime", "creation", "name"))
    source_names = _reconciliation_source_names(company)
    new_rows = [
        sle for sle in ledger
        if sle.voucher_type in {"Stock Entry", "Stock Reconciliation"}
        and _entry_name(sle, source_names) in new_names
    ]
    if {_entry_name(sle, source_names) for sle in new_rows} != new_names or any(
        sle.is_cancelled or sle.qty_after_transaction != ZERO for sle in new_rows
    ):
        raise ValidationError("Backdated Stock Entries need unvalued staged ledger rows.")
    if any(sle.voucher_type not in {"Stock Entry", "Stock Reconciliation"} for sle in ledger):
        raise ValidationError("Backdated replay does not support other stock voucher types yet.")
    names = {_entry_name(sle, source_names) for sle in ledger}
    documents = {
        entry.pk: entry for entry in StockEntry.objects.select_for_update().filter(
            company=company, pk__in=names
        )
    }
    if names != set(documents) or any(
        (entry.status != StockEntry.Status.DRAFT if name in new_names else
         entry.status not in (StockEntry.Status.SUBMITTED, StockEntry.Status.CANCELLED))
        for name, entry in documents.items()
    ):
        raise ValidationError("Stock ledger contains an unsupported or missing source voucher.")

    bins = {
        (item_bin.item_id, item_bin.warehouse_id): item_bin
        for item_bin in Bin.objects.select_for_update().filter(company=company)
    }
    last = {}
    for sle in ledger:
        if not sle.is_cancelled and _entry_name(sle, source_names) not in new_names:
            last[(sle.item_id, sle.warehouse_id)] = sle
    for key, item_bin in bins.items():
        latest = last.get(key)
        if item_bin.actual_qty != (latest.qty_after_transaction if latest else ZERO) or item_bin.stock_value != (latest.stock_value if latest else ZERO):
            raise ValidationError("Bin and stock ledger disagree; backdated posting was not applied.")
    if any(key not in bins for key in last):
        raise ValidationError("A stock ledger balance has no Bin; backdated posting was not applied.")

    original = {sle.pk: copy(sle) for sle in ledger}
    affected, states = _revalue_ledger(company, ledger, source_names,
                                      new_vouchers=new_names)
    _rebuild_bins(bins, states)
    active = _repost_affected(company, ledger, original, documents, affected, user,
                              source_names)
    _refresh_reconciliation_values(company, ledger, source_names)
    return {name: active[name] for name in new_names}


def replay_new_stock_entry(stock_entry, *, user=None):
    """Value one staged backdated Stock Entry and correct affected vouchers."""
    return replay_new_stock_entries((stock_entry,), user=user)[stock_entry.pk]


@transaction.atomic
def submit_receipt_rate_correction(correction, *, user=None):
    """Amend one submitted receipt rate and replay all dependent stock and GL."""
    if not isinstance(correction, ReceiptRateCorrection) or not correction.pk:
        raise TypeError("correction must be a saved ReceiptRateCorrection")
    company_id = ReceiptRateCorrection.objects.select_related(
        "stock_entry_detail__stock_entry"
    ).get(pk=correction.pk).stock_entry_detail.stock_entry.company_id
    company = Company.objects.select_for_update().get(pk=company_id)
    correction = ReceiptRateCorrection.objects.select_for_update().get(pk=correction.pk)
    if correction.status != ReceiptRateCorrection.Status.DRAFT:
        raise ValidationError("Only a draft rate correction can be submitted.")
    correction.full_clean()
    detail = StockEntryDetail.objects.select_for_update().get(pk=correction.stock_entry_detail_id)
    entry = StockEntry.objects.select_for_update().get(pk=detail.stock_entry_id)
    if entry.company_id != company.pk or entry.status != StockEntry.Status.SUBMITTED or entry.purpose != StockEntryType.Purpose.MATERIAL_RECEIPT:
        raise ValidationError("Rate correction requires a submitted Material Receipt in this company.")
    _check_open_period(company, entry, user)

    ledger = list(StockLedgerEntry.objects.select_for_update().filter(
        company=company
    ).order_by("posting_datetime", "creation", "name"))
    source_names = _reconciliation_source_names(company)
    if any(sle.voucher_type not in {"Stock Entry", "Stock Reconciliation"} for sle in ledger):
        raise ValidationError("Rate correction does not support other stock voucher types yet.")
    target = [
        sle for sle in ledger
        if sle.voucher_no == entry.pk and sle.voucher_detail_no == f"{detail.pk}:IN"
        and not sle.is_cancelled
    ]
    if len(target) != 1 or target[0].actual_qty <= ZERO or target[0].dependant_sle_voucher_detail_no:
        raise ValidationError("The receipt row has no independent incoming ledger entry.")
    source = target[0]
    if source.incoming_rate == correction.new_rate:
        raise ValidationError("The receipt already has this valuation rate.")
    if detail.basic_rate != source.incoming_rate:
        raise ValidationError("Receipt row and ledger rates disagree; reconcile before correction.")

    names = {_entry_name(sle, source_names) for sle in ledger}
    documents = {
        document.pk: document for document in StockEntry.objects.select_for_update().filter(
            company=company, pk__in=names
        )
    }
    if names != set(documents) or any(
        document.status not in (StockEntry.Status.SUBMITTED, StockEntry.Status.CANCELLED)
        for document in documents.values()
    ):
        raise ValidationError("Stock ledger contains an unsupported or missing source voucher.")
    bins = {
        (item_bin.item_id, item_bin.warehouse_id): item_bin
        for item_bin in Bin.objects.select_for_update().filter(company=company)
    }
    last = {}
    for sle in ledger:
        if not sle.is_cancelled:
            last[(sle.item_id, sle.warehouse_id)] = sle
    for key, item_bin in bins.items():
        latest = last.get(key)
        if item_bin.actual_qty != (latest.qty_after_transaction if latest else ZERO) or item_bin.stock_value != (latest.stock_value if latest else ZERO):
            raise ValidationError("Bin and stock ledger disagree; correction was not applied.")
    if any(key not in bins for key in last):
        raise ValidationError("A stock ledger balance has no Bin; correction was not applied.")

    original = {sle.pk: copy(sle) for sle in ledger}
    old_rate = source.incoming_rate
    source.incoming_rate = correction.new_rate
    source.save(_allow_repost=True, update_fields=("incoming_rate",))
    affected, states = _revalue_ledger(company, ledger, source_names)
    _rebuild_bins(bins, states)
    _repost_affected(company, ledger, original, documents, affected, user, source_names)
    _refresh_reconciliation_values(company, ledger, source_names)

    correction.previous_rate = old_rate
    correction.status = ReceiptRateCorrection.Status.SUBMITTED
    correction.submitted_at = timezone.now()
    correction.save(
        _submitting=True, update_fields=("previous_rate", "status", "submitted_at")
    )
    return correction


@transaction.atomic
def cancel_stock_entry(stock_entry, *, user=None, _from_reconciliation=False):
    """Cancel a submitted voucher; replay active stock and append GL reversals/deltas."""
    if not isinstance(stock_entry, StockEntry) or not stock_entry.pk:
        raise TypeError("stock_entry must be a saved StockEntry")
    company = Company.objects.select_for_update().get(pk=stock_entry.company_id)
    stock_entry = StockEntry.objects.select_for_update().get(pk=stock_entry.pk)
    if stock_entry.status != StockEntry.Status.SUBMITTED:
        raise ValidationError("Only a submitted Stock Entry can be cancelled.")
    if not _from_reconciliation and (
        StockReconciliation.objects.filter(
            status=StockReconciliation.Status.SUBMITTED, receipt_entry_id=stock_entry.pk
        ).exists()
        or StockReconciliation.objects.filter(
            status=StockReconciliation.Status.SUBMITTED, issue_entry_id=stock_entry.pk
        ).exists()
    ):
        raise ValidationError("Cancel the source Stock Reconciliation instead.")
    _check_open_period(company, stock_entry, user)

    ledger = list(StockLedgerEntry.objects.select_for_update().filter(
        company=company
    ).order_by("posting_datetime", "creation", "name"))
    source_names = _reconciliation_source_names(company)
    target_rows = [
        sle for sle in ledger
        if _entry_name(sle, source_names) == stock_entry.pk
        and sle.voucher_type in {"Stock Entry", "Stock Reconciliation"}
    ]
    if not target_rows:
        raise ValidationError("Stock Entry has no stock ledger rows to cancel.")
    if any(sle.is_cancelled for sle in target_rows):
        raise ValidationError("Stock Entry ledger rows are already partly cancelled.")
    if any(sle.voucher_type not in {"Stock Entry", "Stock Reconciliation"} for sle in ledger):
        raise ValidationError("Cancellation replay does not support other stock voucher types yet.")

    names = {_entry_name(sle, source_names) for sle in ledger}
    documents = {
        entry.pk: entry for entry in StockEntry.objects.select_for_update().filter(
            company=company, pk__in=names
        )
    }
    if names != set(documents) or any(
        entry.status not in (StockEntry.Status.SUBMITTED, StockEntry.Status.CANCELLED)
        for entry in documents.values()
    ):
        raise ValidationError("Stock ledger contains an unsupported or missing source voucher.")

    bins = {
        (item_bin.item_id, item_bin.warehouse_id): item_bin
        for item_bin in Bin.objects.select_for_update().filter(company=company)
    }
    last = {}
    for sle in ledger:
        if not sle.is_cancelled:
            last[(sle.item_id, sle.warehouse_id)] = sle
    for key, item_bin in bins.items():
        latest = last.get(key)
        if item_bin.actual_qty != (latest.qty_after_transaction if latest else ZERO) or item_bin.stock_value != (latest.stock_value if latest else ZERO):
            raise ValidationError("Bin and stock ledger disagree; cancellation was not applied.")
    if any(key not in bins for key in last):
        raise ValidationError("A stock ledger balance has no Bin; cancellation was not applied.")

    original = {sle.pk: copy(sle) for sle in ledger}
    for sle in ledger:
        if _entry_name(sle, source_names) == stock_entry.pk and not sle.is_cancelled:
            sle.is_cancelled = True
            sle.save(_allow_repost=True, update_fields=("is_cancelled",))
    affected, states = _revalue_ledger(company, ledger, source_names)

    _rebuild_bins(bins, states)

    voucher_type, voucher_no = _voucher_identities(ledger, source_names)[stock_entry.pk]
    original_gl = list(GLEntry.objects.filter(
        company=company, voucher_type=voucher_type, voucher_no=voucher_no
    )) + list(GLEntry.objects.filter(
        company=company, voucher_type="Stock Valuation Repost",
        against_voucher_type=voucher_type, against_voucher=voucher_no,
    ))
    if original_gl:
        post_gl_entries(
            company=company, posting_date=stock_entry.posting_date,
            voucher_type=f"{voucher_type} Cancellation", voucher_no=voucher_no,
            is_opening=stock_entry.is_opening, user=user,
            lines=[LedgerLine(
                account=row.account, debit=row.credit, credit=row.debit,
                cost_center=row.cost_center, project=row.project,
                finance_book=row.finance_book, remarks="Stock Entry cancellation",
                against_voucher_type=voucher_type, against_voucher=voucher_no,
            ) for row in original_gl],
        )

    _repost_affected(company, ledger, original, documents, affected, user, source_names)
    _refresh_reconciliation_values(company, ledger, source_names)

    stock_entry.status = StockEntry.Status.CANCELLED
    stock_entry.save(_allow_repost=True, update_fields=("status",))
    return stock_entry
