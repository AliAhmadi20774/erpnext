"""Submit the supported Stock Entry purposes through the stock ledger service."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from organizations.models import Company

from .ledger import StockLedgerLine, post_stock_entries
from .models import StockEntry, StockEntryType


ZERO = Decimal("0")


@transaction.atomic
def submit_stock_entry(stock_entry):
    if not isinstance(stock_entry, StockEntry) or not stock_entry.pk:
        raise TypeError("stock_entry must be a saved StockEntry")

    company = Company.objects.select_for_update().get(pk=stock_entry.company_id)
    if company.enable_perpetual_inventory:
        raise ValidationError(
            "Stock Entry submission with perpetual inventory requires GL integration."
        )
    stock_entry = StockEntry.objects.select_for_update().select_related(
        "company", "stock_entry_type", "project"
    ).get(pk=stock_entry.pk)
    if stock_entry.status != StockEntry.Status.DRAFT:
        raise ValidationError("Stock entry has already been submitted.")
    stock_entry.full_clean()
    if stock_entry.is_opening and stock_entry.purpose != StockEntryType.Purpose.MATERIAL_RECEIPT:
        raise ValidationError("Only Material Receipt can be marked as an opening stock entry.")

    rows = list(
        stock_entry.items.select_related(
            "item",
            "item__stock_uom",
            "uom",
            "source_warehouse",
            "target_warehouse",
            "project",
        ).order_by("position", "id")
    )
    if not rows:
        raise ValidationError("A stock entry requires at least one item row.")
    for row in rows:
        row.full_clean()

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
                    quantity=row.transfer_qty,
                    incoming_rate=(
                        None
                        if outgoing_detail
                        else row.basic_rate
                    ),
                    project=row.project or stock_entry.project,
                    voucher_detail_no=f"{row.pk}:IN",
                    rate_from_voucher_detail_no=outgoing_detail,
                )
            )

    entries = post_stock_entries(
        company=company,
        posting_date=stock_entry.posting_date,
        posting_time=stock_entry.posting_time,
        voucher_type="Stock Entry",
        voucher_no=stock_entry.name,
        lines=lines,
    )
    by_detail = {entry.voucher_detail_no: entry for entry in entries}

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

    incoming_value = sum(
        (entry.stock_value_difference for entry in entries if entry.actual_qty > ZERO), ZERO
    )
    outgoing_value = sum(
        (-entry.stock_value_difference for entry in entries if entry.actual_qty < ZERO), ZERO
    )
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
