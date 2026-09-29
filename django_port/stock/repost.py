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

from .entries import _gl_lines
from .ledger import _decimal, _normalise_queue, _queue_valuation, _serialise_queue
from .models import (
    Bin, ReceiptRateCorrection, StockEntry, StockEntryDetail, StockLedgerEntry,
    StockEntryType,
)


ZERO = Decimal("0")
VALUATION_FIELDS = (
    "qty_after_transaction", "incoming_rate", "outgoing_rate", "valuation_rate",
    "stock_value", "stock_value_difference", "stock_queue",
)


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


def _gl_difference(old_lines, new_lines, voucher_name):
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
                against_voucher_type="Stock Entry", against_voucher=voucher_name,
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
    entry.total_incoming_value = sum(
        (sle.stock_value_difference for sle in by_detail.values() if sle.actual_qty > ZERO), ZERO
    )
    entry.total_outgoing_value = sum(
        (-sle.stock_value_difference for sle in by_detail.values() if sle.actual_qty < ZERO), ZERO
    )
    entry.value_difference = entry.total_incoming_value - entry.total_outgoing_value
    entry.total_amount = total
    entry.save(_allow_repost=True, update_fields=(
        "total_incoming_value", "total_outgoing_value", "value_difference", "total_amount",
    ))


def _revalue_ledger(company, ledger, *, new_voucher=""):
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
        incoming_rate = sle.incoming_rate if quantity > ZERO else ZERO
        if sle.dependant_sle_voucher_detail_no:
            source = outgoing.get((sle.voucher_no, sle.dependant_sle_voucher_detail_no))
            if source is None or quantity <= ZERO or -source[0].actual_qty != quantity or source[0].item_id != sle.item_id:
                raise ValidationError("A transfer source is missing during stock replay.")
            incoming_rate = source[0].outgoing_rate
            incoming_layers = source[1]
        consumed_layers = []
        if company.valuation_method == Company.ValuationMethod.MOVING_AVERAGE:
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
            if sle.voucher_no != new_voucher:
                affected.add(sle.voucher_no)
        states[key] = (new_qty, new_value, values["valuation_rate"], new_queue)
        if quantity < ZERO:
            outgoing[(sle.voucher_no, sle.voucher_detail_no)] = (sle, consumed_layers)
    return affected, states


def _rebuild_bins(bins, states):
    for key, item_bin in bins.items():
        quantity, value, rate, _ = states.get(key, (ZERO, ZERO, ZERO, []))
        if (item_bin.actual_qty, item_bin.stock_value, item_bin.valuation_rate) != (quantity, value, rate):
            item_bin.actual_qty = quantity
            item_bin.stock_value = value
            item_bin.valuation_rate = rate
            item_bin.save(_allow_stock_write=True)


def _repost_affected(company, ledger, original, documents, affected, user):
    active = defaultdict(dict)
    old_active = defaultdict(dict)
    for sle in ledger:
        if not sle.is_cancelled:
            active[sle.voucher_no][sle.voucher_detail_no] = sle
            old_active[sle.voucher_no][sle.voucher_detail_no] = original[sle.pk]
    for name in affected:
        entry = documents[name]
        _check_open_period(company, entry, user)
        rows = list(entry.items.select_related(
            "source_warehouse", "target_warehouse", "expense_account", "cost_center", "project"
        ).order_by("position", "id"))
        if entry.perpetual_inventory_at_submit:
            existing_gl = list(GLEntry.objects.filter(
                company=company, voucher_type="Stock Entry", voucher_no=name,
            )) + list(GLEntry.objects.filter(
                company=company, voucher_type="Stock Valuation Repost",
                against_voucher_type="Stock Entry", against_voucher=name,
            ))
            original_lines = _gl_lines(entry, company, rows, old_active[name])
            if _signed_gl_totals(existing_gl) != _signed_gl_totals(original_lines):
                raise ValidationError(
                    "Existing GL does not match this Stock Entry valuation; historical accounting dimensions need reconciliation."
                )
            delta = _gl_difference(
                original_lines, _gl_lines(entry, company, rows, active[name]), name,
            )
            if delta:
                post_gl_entries(
                    company=company, posting_date=entry.posting_date,
                    voucher_type="Stock Valuation Repost",
                    voucher_no=f"STOCK-RPV-{uuid4().hex}",
                    lines=delta, is_opening=entry.is_opening, user=user,
                )
        elif GLEntry.objects.filter(
            company=company, voucher_type="Stock Entry", voucher_no=name
        ).exists():
            raise ValidationError("The historical Stock Entry perpetual-inventory setting is missing.")
        _update_entry_totals(entry, rows, active[name])
    return active


@transaction.atomic
def replay_new_stock_entry(stock_entry, *, user=None):
    """Value a staged backdated Stock Entry and correct affected future vouchers."""
    company = Company.objects.select_for_update().get(pk=stock_entry.company_id)
    ledger = list(StockLedgerEntry.objects.select_for_update().filter(
        company=company
    ).order_by("posting_datetime", "creation", "name"))
    new_rows = [
        sle for sle in ledger
        if sle.voucher_type == "Stock Entry" and sle.voucher_no == stock_entry.pk
    ]
    if not new_rows or any(sle.is_cancelled or sle.qty_after_transaction != ZERO for sle in new_rows):
        raise ValidationError("Backdated Stock Entry needs unvalued staged ledger rows.")
    if any(sle.voucher_type != "Stock Entry" for sle in ledger):
        raise ValidationError("Backdated replay does not support other stock voucher types yet.")
    names = {sle.voucher_no for sle in ledger}
    documents = {
        entry.pk: entry for entry in StockEntry.objects.select_for_update().filter(
            company=company, pk__in=names
        )
    }
    if names != set(documents) or any(
        (entry.status != StockEntry.Status.DRAFT if name == stock_entry.pk else
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
        if not sle.is_cancelled and sle.voucher_no != stock_entry.pk:
            last[(sle.item_id, sle.warehouse_id)] = sle
    for key, item_bin in bins.items():
        latest = last.get(key)
        if item_bin.actual_qty != (latest.qty_after_transaction if latest else ZERO) or item_bin.stock_value != (latest.stock_value if latest else ZERO):
            raise ValidationError("Bin and stock ledger disagree; backdated posting was not applied.")
    if any(key not in bins for key in last):
        raise ValidationError("A stock ledger balance has no Bin; backdated posting was not applied.")

    original = {sle.pk: copy(sle) for sle in ledger}
    affected, states = _revalue_ledger(company, ledger, new_voucher=stock_entry.pk)
    _rebuild_bins(bins, states)
    active = _repost_affected(company, ledger, original, documents, affected, user)
    return active[stock_entry.pk]


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
    if any(sle.voucher_type != "Stock Entry" for sle in ledger):
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

    names = {sle.voucher_no for sle in ledger}
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
    affected, states = _revalue_ledger(company, ledger)
    _rebuild_bins(bins, states)
    _repost_affected(company, ledger, original, documents, affected, user)

    correction.previous_rate = old_rate
    correction.status = ReceiptRateCorrection.Status.SUBMITTED
    correction.submitted_at = timezone.now()
    correction.save(
        _submitting=True, update_fields=("previous_rate", "status", "submitted_at")
    )
    return correction


@transaction.atomic
def cancel_stock_entry(stock_entry, *, user=None):
    """Cancel a submitted voucher; replay active stock and append GL reversals/deltas."""
    if not isinstance(stock_entry, StockEntry) or not stock_entry.pk:
        raise TypeError("stock_entry must be a saved StockEntry")
    company = Company.objects.select_for_update().get(pk=stock_entry.company_id)
    stock_entry = StockEntry.objects.select_for_update().get(pk=stock_entry.pk)
    if stock_entry.status != StockEntry.Status.SUBMITTED:
        raise ValidationError("Only a submitted Stock Entry can be cancelled.")
    _check_open_period(company, stock_entry, user)

    ledger = list(StockLedgerEntry.objects.select_for_update().filter(
        company=company
    ).order_by("posting_datetime", "creation", "name"))
    target_rows = [
        sle for sle in ledger
        if sle.voucher_no == stock_entry.pk and sle.voucher_type == "Stock Entry"
    ]
    if not target_rows:
        raise ValidationError("Stock Entry has no stock ledger rows to cancel.")
    if any(sle.is_cancelled for sle in target_rows):
        raise ValidationError("Stock Entry ledger rows are already partly cancelled.")
    if any(sle.voucher_type != "Stock Entry" for sle in ledger):
        raise ValidationError("Cancellation replay does not support other stock voucher types yet.")

    names = {sle.voucher_no for sle in ledger}
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
        if sle.voucher_no == stock_entry.pk and not sle.is_cancelled:
            sle.is_cancelled = True
            sle.save(_allow_repost=True, update_fields=("is_cancelled",))
    affected, states = _revalue_ledger(company, ledger)

    _rebuild_bins(bins, states)

    original_gl = list(GLEntry.objects.filter(
        company=company, voucher_type="Stock Entry", voucher_no=stock_entry.pk
    )) + list(GLEntry.objects.filter(
        company=company, voucher_type="Stock Valuation Repost",
        against_voucher_type="Stock Entry", against_voucher=stock_entry.pk,
    ))
    if original_gl:
        post_gl_entries(
            company=company, posting_date=stock_entry.posting_date,
            voucher_type="Stock Entry Cancellation", voucher_no=stock_entry.pk,
            is_opening=stock_entry.is_opening, user=user,
            lines=[LedgerLine(
                account=row.account, debit=row.credit, credit=row.debit,
                cost_center=row.cost_center, project=row.project,
                finance_book=row.finance_book, remarks="Stock Entry cancellation",
                against_voucher_type="Stock Entry", against_voucher=stock_entry.pk,
            ) for row in original_gl],
        )

    _repost_affected(company, ledger, original, documents, affected, user)

    stock_entry.status = StockEntry.Status.CANCELLED
    stock_entry.save(_allow_repost=True, update_fields=("status",))
    return stock_entry
