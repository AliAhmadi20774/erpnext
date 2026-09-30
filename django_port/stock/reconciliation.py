"""Stock counts and current-value resets backed by the Stock Entry workflow."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from organizations.models import Company

from .entries import _finish_stock_entry, _prepare_stock_entry, submit_stock_entry
from .ledger import _STOCK_ENTRY_REPLAY_TOKEN, _decimal, _posting_datetime, post_stock_entries
from .models import (
    Bin, StockEntry, StockEntryDetail, StockEntryType, StockLedgerEntry,
    StockReconciliation,
)
from .repost import _check_open_period, cancel_stock_entry, replay_new_stock_entries


ZERO = Decimal("0")


def _entry_type(purpose):
    entry_type = StockEntryType.objects.filter(
        purpose=purpose, add_to_transit=False
    ).order_by("name").first()
    if entry_type is None:
        raise ValidationError("Create standard Material Receipt and Material Issue types first.")
    return entry_type


@transaction.atomic
def submit_stock_reconciliation(reconciliation, *, user=None):
    if not isinstance(reconciliation, StockReconciliation) or not reconciliation.pk:
        raise TypeError("reconciliation must be a saved StockReconciliation")
    company = Company.objects.select_for_update().get(pk=reconciliation.company_id)
    reconciliation = StockReconciliation.objects.select_for_update().get(pk=reconciliation.pk)
    if reconciliation.status != StockReconciliation.Status.DRAFT:
        raise ValidationError("Only a draft stock reconciliation can be submitted.")
    reconciliation.full_clean()
    _check_open_period(company, reconciliation, user)
    rows = list(reconciliation.items.select_related(
        "item", "item__stock_uom", "warehouse"
    ).order_by("position", "id"))
    if not rows:
        raise ValidationError("A stock reconciliation needs at least one counted item.")
    posting_datetime = _posting_datetime(reconciliation.posting_date, reconciliation.posting_time)
    increases = []
    decreases = []
    direct_rows = set()
    backdated = False
    for row in rows:
        row.full_clean()
        latest = StockLedgerEntry.objects.filter(
            item=row.item, warehouse=row.warehouse, is_cancelled=False
        ).order_by("-posting_datetime", "-creation", "-name").first()
        if latest and latest.posting_datetime > posting_datetime:
            backdated = True
        item_bin = Bin.objects.select_for_update().filter(
            item=row.item, warehouse=row.warehouse
        ).first()
        if latest and item_bin is None:
            raise ValidationError("A stock ledger balance has no Bin.")
        bin_qty = _decimal(item_bin.actual_qty if item_bin else ZERO)
        bin_value = _decimal(item_bin.stock_value if item_bin else ZERO)
        if bin_qty != (latest.qty_after_transaction if latest else ZERO) or bin_value != (latest.stock_value if latest else ZERO):
            raise ValidationError("Bin and stock ledger disagree; reconcile the balance first.")
        as_of = StockLedgerEntry.objects.filter(
            item=row.item, warehouse=row.warehouse, is_cancelled=False,
            posting_datetime__lte=posting_datetime,
        ).order_by("-posting_datetime", "-creation", "-name").first()
        current_qty = _decimal(as_of.qty_after_transaction if as_of else ZERO)
        current_value = _decimal(as_of.stock_value if as_of else ZERO)
        difference = _decimal(row.counted_qty - current_qty)
        if difference > ZERO:
            if row.revalue_existing_stock:
                raise ValidationError(f"Row {row.position}: value reset requires unchanged quantity.")
            if row.receipt_rate == ZERO and not row.allow_zero_valuation_rate:
                raise ValidationError(f"Row {row.position} needs a receipt rate for an increase.")
            increases.append((row, difference))
        elif difference < ZERO:
            if row.revalue_existing_stock:
                raise ValidationError(f"Row {row.position}: value reset requires unchanged quantity.")
            if row.receipt_rate != ZERO:
                raise ValidationError(f"Row {row.position}: receipt rate applies only to increases.")
            decreases.append((row, -difference))
        elif row.revalue_existing_stock:
            if current_qty <= ZERO:
                raise ValidationError(f"Row {row.position}: value reset requires stock on hand.")
            if row.receipt_rate == ZERO and not row.allow_zero_valuation_rate:
                raise ValidationError(f"Row {row.position}: zero target rate requires explicit allowance.")
            if row.direct_value_adjustment:
                direct_rows.add(row.pk)
            else:
                # The paired method drains old layers and restores the same
                # quantity at the target rate in issue-first order.
                decreases.append((row, current_qty))
            increases.append((row, current_qty))
        elif row.receipt_rate != ZERO:
            raise ValidationError(
                f"Row {row.position}: select value reset to change only the valuation rate."
            )
        row.previous_qty = current_qty
        row.difference_qty = difference
        row.previous_valuation_rate = _decimal(as_of.valuation_rate if as_of else ZERO)
        row.previous_stock_value = current_value

    if not increases and not decreases:
        raise ValidationError("The counted quantities do not change any stock balance.")
    grouped_replay = backdated and any(row.revalue_existing_stock for row in rows)

    created = {}
    prepared = {}
    for purpose, selected in (
        (StockEntryType.Purpose.MATERIAL_ISSUE, decreases),
        (StockEntryType.Purpose.MATERIAL_RECEIPT, increases),
    ):
        if not selected:
            continue
        entry = StockEntry.objects.create(
            company=company, stock_entry_type=_entry_type(purpose),
            posting_date=reconciliation.posting_date,
            posting_time=reconciliation.posting_time,
            cost_center=reconciliation.cost_center,
            remarks=f"Stock Reconciliation {reconciliation.pk}: {reconciliation.remarks}".strip(),
        )
        for position, (row, quantity) in enumerate(selected, 1):
            StockEntryDetail.objects.create(
                stock_entry=entry, position=position, item=row.item,
                source_warehouse=row.warehouse if purpose == StockEntryType.Purpose.MATERIAL_ISSUE else None,
                target_warehouse=row.warehouse if purpose == StockEntryType.Purpose.MATERIAL_RECEIPT else None,
                qty=quantity, uom=row.item.stock_uom, conversion_factor=Decimal("1"),
                basic_rate=row.receipt_rate if purpose == StockEntryType.Purpose.MATERIAL_RECEIPT else ZERO,
                allow_zero_valuation_rate=row.allow_zero_valuation_rate,
                is_value_adjustment=row.pk in direct_rows,
                expense_account=reconciliation.expense_account,
            )
        if grouped_replay:
            prepared[purpose] = _prepare_stock_entry(entry, company, user)
            created[purpose] = prepared[purpose][0]
        elif purpose == StockEntryType.Purpose.MATERIAL_RECEIPT and direct_rows:
            prepared_entry, entry_rows, lines = _prepare_stock_entry(entry, company, user)
            valued = post_stock_entries(
                company=company, posting_date=entry.posting_date,
                posting_time=entry.posting_time, voucher_type="Stock Reconciliation",
                voucher_no=entry.pk, lines=lines,
            )
            created[purpose] = _finish_stock_entry(
                prepared_entry, company, entry_rows,
                {sle.voucher_detail_no: sle for sle in valued}, user,
            )
        else:
            created[purpose] = submit_stock_entry(entry, user=user)

    if grouped_replay:
        # The issue and receipt must coexist before historical replay: replaying
        # only the issue can temporarily make a valid future voucher negative.
        for purpose, (entry, _entry_rows, lines) in prepared.items():
            post_stock_entries(
                company=company,
                posting_date=entry.posting_date,
                posting_time=entry.posting_time,
                voucher_type=("Stock Reconciliation" if purpose == StockEntryType.Purpose.MATERIAL_RECEIPT
                              and direct_rows else "Stock Entry"),
                voucher_no=entry.pk,
                lines=lines,
                _defer_replay=_STOCK_ENTRY_REPLAY_TOKEN,
            )
        valued = replay_new_stock_entries(
            [entry for entry, _entry_rows, _lines in prepared.values()], user=user,
        )
        for purpose, (entry, entry_rows, _lines) in prepared.items():
            created[purpose] = _finish_stock_entry(
                entry, company, entry_rows, valued[entry.pk], user,
            )

    created_names = [entry.pk for entry in created.values()]
    for row in rows:
        entries = StockLedgerEntry.objects.filter(
            voucher_type__in=("Stock Entry", "Stock Reconciliation"),
            voucher_no__in=created_names,
            item=row.item, warehouse=row.warehouse, is_cancelled=False,
        )
        row.value_difference = _decimal(sum(
            (sle.stock_value_difference for sle in entries), ZERO,
        ))
        row.save(_submitting=True, update_fields=(
            "previous_qty", "difference_qty", "previous_valuation_rate",
            "previous_stock_value", "value_difference",
        ))
    reconciliation.receipt_entry = created.get(StockEntryType.Purpose.MATERIAL_RECEIPT)
    reconciliation.issue_entry = created.get(StockEntryType.Purpose.MATERIAL_ISSUE)
    reconciliation.total_increase_qty = sum(
        (row.difference_qty for row in rows if row.difference_qty > ZERO), ZERO
    )
    reconciliation.total_decrease_qty = sum(
        (-row.difference_qty for row in rows if row.difference_qty < ZERO), ZERO
    )
    reconciliation.total_value_difference = sum((row.value_difference for row in rows), ZERO)
    reconciliation.status = StockReconciliation.Status.SUBMITTED
    reconciliation.save(_lifecycle=True, update_fields=(
        "receipt_entry", "issue_entry", "total_increase_qty", "total_decrease_qty",
        "total_value_difference", "status",
    ))
    return reconciliation


@transaction.atomic
def cancel_stock_reconciliation(reconciliation, *, user=None):
    if not isinstance(reconciliation, StockReconciliation) or not reconciliation.pk:
        raise TypeError("reconciliation must be a saved StockReconciliation")
    company = Company.objects.select_for_update().get(pk=reconciliation.company_id)
    reconciliation = StockReconciliation.objects.select_for_update().get(pk=reconciliation.pk)
    if reconciliation.status != StockReconciliation.Status.SUBMITTED:
        raise ValidationError("Only a submitted stock reconciliation can be cancelled.")
    _check_open_period(company, reconciliation, user)
    reconciliation.status = StockReconciliation.Status.CANCELLED
    reconciliation.save(_lifecycle=True, update_fields=("status",))
    for entry in (reconciliation.issue_entry, reconciliation.receipt_entry):
        if entry:
            cancel_stock_entry(entry, user=user, _from_reconciliation=True)
    return reconciliation
