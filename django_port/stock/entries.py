"""Submit the supported Stock Entry purposes through the stock ledger service."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from accounting.ledger import LedgerLine, post_gl_entries
from accounting.models import PeriodClosingVoucher
from accounting.periods import validate_accounting_period
from organizations.models import Company

from .ledger import StockLedgerLine, _posting_datetime, _STOCK_ENTRY_REPLAY_TOKEN, post_stock_entries
from .models import StockEntry, StockEntryType, StockLedgerEntry


ZERO = Decimal("0")


def _inventory_account(warehouse, company):
    account = warehouse.effective_account()
    if (
        account.company_id != company.pk
        or account.is_group
        or account.disabled
        or account.account_type != "Stock"
        or account.account_currency_id != company.default_currency_id
    ):
        raise ValidationError(
            f"Warehouse {warehouse.pk} needs an enabled Stock account in the company currency."
        )
    return account


def _difference_account(row, company):
    account = row.expense_account or company.stock_adjustment_account
    if account is None:
        raise ValidationError(
            f"Set a Difference Account on row {row.position} or a Stock Adjustment Account for {company.pk}."
        )
    if (
        account.company_id != company.pk
        or account.is_group
        or account.disabled
        or account.account_type == "Stock"
        or account.account_currency_id != company.default_currency_id
    ):
        raise ValidationError("Difference Account must be an enabled non-Stock ledger in the company currency.")
    return account


def _gl_lines(stock_entry, company, rows, by_detail):
    lines = []
    for row in rows:
        outgoing = by_detail.get(f"{row.pk}:OUT")
        incoming = by_detail.get(f"{row.pk}:IN")
        source = _inventory_account(row.source_warehouse, company) if outgoing else None
        target = _inventory_account(row.target_warehouse, company) if incoming else None
        if outgoing and incoming:
            debit_account = target
            credit_account = source
            debit_amount = incoming.stock_value_difference
            credit_amount = -outgoing.stock_value_difference
            if debit_amount != credit_amount:
                raise ValidationError("Transfer stock values must balance before GL posting.")
            if debit_account == credit_account:
                continue
            amount = debit_amount
        elif incoming and incoming.is_value_reset:
            difference_account = _difference_account(row, company)
            if stock_entry.is_opening and difference_account.report_type == "Profit and Loss":
                raise ValidationError("Opening stock adjustments require a Balance Sheet Difference Account.")
            difference = incoming.stock_value_difference
            debit_account, credit_account = (
                (target, difference_account) if difference >= ZERO
                else (difference_account, target)
            )
            amount = abs(difference)
        elif incoming:
            debit_account = target
            credit_account = _difference_account(row, company)
            if stock_entry.is_opening and credit_account.report_type == "Profit and Loss":
                raise ValidationError("Opening stock receipts require a Balance Sheet Difference Account.")
            amount = incoming.stock_value_difference
        else:
            debit_account = _difference_account(row, company)
            credit_account = source
            amount = -outgoing.stock_value_difference

        if amount < ZERO:
            raise ValidationError("Stock value difference cannot create a negative GL amount.")
        if amount == ZERO:
            continue

        cost_center = row.cost_center or stock_entry.cost_center or company.cost_center
        project = row.project or stock_entry.project
        common = {
            "cost_center": cost_center,
            "project": project,
            "finance_book": stock_entry.finance_book,
            "remarks": stock_entry.remarks,
        }
        lines.extend(
            (
                LedgerLine(account=debit_account, debit=amount, **common),
                LedgerLine(account=credit_account, credit=amount, **common),
            )
        )
    return lines


def _stock_value_totals(entries):
    """Include zero-quantity valuation adjustments in the signed entry totals."""
    incoming = ZERO
    outgoing = ZERO
    for entry in entries:
        difference = entry.stock_value_difference
        if entry.actual_qty > ZERO or (entry.is_value_reset and difference > ZERO):
            incoming += difference
        elif entry.actual_qty < ZERO or (entry.is_value_reset and difference < ZERO):
            outgoing -= difference
    return incoming, outgoing


def _prepare_stock_entry(stock_entry, company, user):
    """Validate and freeze submit-time settings before stock lines are posted."""
    if not isinstance(stock_entry, StockEntry) or not stock_entry.pk:
        raise TypeError("stock_entry must be a saved StockEntry")
    if stock_entry.company_id != company.pk:
        raise ValidationError("Stock Entry must belong to the locked company.")

    stock_entry = StockEntry.objects.select_for_update().select_related(
        "company", "stock_entry_type", "project", "cost_center"
    ).get(pk=stock_entry.pk)
    if stock_entry.status != StockEntry.Status.DRAFT:
        raise ValidationError("Stock entry has already been submitted.")
    stock_entry.full_clean()
    if stock_entry.is_opening and stock_entry.purpose != StockEntryType.Purpose.MATERIAL_RECEIPT:
        raise ValidationError("Only Material Receipt can be marked as an opening stock entry.")
    validate_accounting_period(
        company=company,
        posting_date=stock_entry.posting_date,
        document_type="Stock Entry",
        user=user,
    )
    if PeriodClosingVoucher.objects.filter(
        company=company,
        status=PeriodClosingVoucher.Status.SUBMITTED,
        period_end_date__gte=stock_entry.posting_date,
    ).exists():
        raise ValidationError("Cannot post on or before a submitted period closing voucher.")

    rows = list(
        stock_entry.items.select_related(
            "item",
            "item__stock_uom",
            "uom",
            "source_warehouse",
            "target_warehouse",
            "project",
            "expense_account",
            "cost_center",
        ).order_by("position", "id")
    )
    if not rows:
        raise ValidationError("A stock entry requires at least one item row.")
    for row in rows:
        row.full_clean()

    # Keep the accounting dimensions used at submit stable for future valuation replay.
    stock_entry.finance_book = company.default_finance_book
    stock_entry.perpetual_inventory_at_submit = company.enable_perpetual_inventory
    stock_entry._submitting = True
    stock_entry.save(update_fields=("finance_book", "perpetual_inventory_at_submit"))
    if company.enable_perpetual_inventory:
        for row in rows:
            changes = []
            if row.expense_account_id is None and company.stock_adjustment_account_id:
                row.expense_account = company.stock_adjustment_account
                changes.append("expense_account")
            if row.cost_center_id is None:
                center = stock_entry.cost_center or company.cost_center
                if center:
                    row.cost_center = center
                    changes.append("cost_center")
            if changes:
                row._submitting = True
                row.save(update_fields=changes)

    lines = []
    purpose = stock_entry.purpose
    if purpose in {
        StockEntryType.Purpose.MATERIAL_ISSUE,
        StockEntryType.Purpose.MATERIAL_TRANSFER,
    }:
        for row in rows:
            detail_no = f"{row.pk}:OUT"
            lines.append(
                StockLedgerLine(
                    item=row.item,
                    warehouse=row.source_warehouse,
                    quantity=-row.transfer_qty,
                    project=row.project or stock_entry.project,
                    voucher_detail_no=detail_no,
                )
            )

    if purpose in {
        StockEntryType.Purpose.MATERIAL_RECEIPT,
        StockEntryType.Purpose.MATERIAL_TRANSFER,
    }:
        for row in rows:
            outgoing_detail = f"{row.pk}:OUT" if purpose == StockEntryType.Purpose.MATERIAL_TRANSFER else ""
            lines.append(
                StockLedgerLine(
                    item=row.item,
                    warehouse=row.target_warehouse,
                    quantity=ZERO if row.is_value_adjustment else row.transfer_qty,
                    incoming_rate=(
                        None
                        if outgoing_detail
                        else row.basic_rate
                    ),
                    project=row.project or stock_entry.project,
                    voucher_detail_no=f"{row.pk}:IN",
                    rate_from_voucher_detail_no=outgoing_detail,
                    is_value_adjustment=row.is_value_adjustment,
                )
            )

    return stock_entry, rows, lines


def _finish_stock_entry(stock_entry, company, rows, by_detail, user,
                        *, voucher_type="Stock Entry", voucher_no=None):
    """Write GL, row snapshots, and totals after every ledger row is valued."""
    entries = tuple(by_detail.values())
    if company.enable_perpetual_inventory:
        gl_lines = _gl_lines(stock_entry, company, rows, by_detail)
        if gl_lines:
            post_gl_entries(
                company=company,
                posting_date=stock_entry.posting_date,
                voucher_type=voucher_type,
                voucher_no=voucher_no or stock_entry.name,
                lines=gl_lines,
                is_opening=stock_entry.is_opening,
                user=user,
            )

    total_amount = ZERO
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
        row._submitting = True
        row.save(
            update_fields=(
                "stock_uom",
                "transfer_qty",
                "basic_rate",
                "basic_amount",
                "amount",
                "actual_qty",
                "valuation_rate",
            )
        )
        total_amount += row.amount

    incoming_value, outgoing_value = _stock_value_totals(entries)
    stock_entry.total_incoming_value = incoming_value
    stock_entry.total_outgoing_value = outgoing_value
    stock_entry.value_difference = incoming_value - outgoing_value
    stock_entry.total_amount = total_amount
    stock_entry.status = StockEntry.Status.SUBMITTED
    stock_entry._submitting = True
    stock_entry.save(
        update_fields=(
            "purpose",
            "total_incoming_value",
            "total_outgoing_value",
            "value_difference",
            "total_amount",
            "status",
        )
    )
    return stock_entry


@transaction.atomic
def submit_stock_entry(stock_entry, *, user=None):
    if not isinstance(stock_entry, StockEntry) or not stock_entry.pk:
        raise TypeError("stock_entry must be a saved StockEntry")
    company = Company.objects.select_for_update().get(pk=stock_entry.company_id)
    stock_entry, rows, lines = _prepare_stock_entry(stock_entry, company, user)
    posting_datetime = _posting_datetime(stock_entry.posting_date, stock_entry.posting_time)
    needs_replay = any(
        StockLedgerEntry.objects.filter(
            company=company, item=line.item, warehouse=line.warehouse,
            is_cancelled=False, posting_datetime__gt=posting_datetime,
        ).exists()
        for line in lines
    )
    entries = post_stock_entries(
        company=company,
        posting_date=stock_entry.posting_date,
        posting_time=stock_entry.posting_time,
        voucher_type="Stock Entry",
        voucher_no=stock_entry.name,
        lines=lines,
        _defer_replay=_STOCK_ENTRY_REPLAY_TOKEN if needs_replay else None,
    )
    if needs_replay:
        from .repost import replay_new_stock_entry

        by_detail = replay_new_stock_entry(stock_entry, user=user)
    else:
        by_detail = {entry.voucher_detail_no: entry for entry in entries}
    return _finish_stock_entry(stock_entry, company, rows, by_detail, user)
