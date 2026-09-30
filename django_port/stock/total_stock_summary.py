"""Current nonzero Bin quantities grouped by company or warehouse and item."""

from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum

from organizations.models import Company

from .models import Bin


@dataclass(frozen=True)
class TotalStockSummaryRow:
    group_name: str
    item_code: str
    description: str
    current_qty: Decimal


@dataclass(frozen=True)
class TotalStockSummaryResult:
    rows: tuple[TotalStockSummaryRow, ...]
    group_by: str


def total_stock_summary(*, group_by="Warehouse", company=None):
    if group_by not in {"Warehouse", "Company"}:
        raise ValidationError("Group by must be Warehouse or Company.")
    if company is not None and not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if group_by == "Warehouse" and company is None:
        raise ValidationError("Select a company for warehouse grouping.")

    bins = Bin.objects.exclude(actual_qty=0)
    if company is not None:
        bins = bins.filter(company=company)
    group_field = "warehouse_id" if group_by == "Warehouse" else "company_id"
    aggregates = bins.values(group_field, "item_id", "item__description").annotate(
        current_qty=Sum("actual_qty"),
    ).order_by(group_field, "item_id")
    rows = tuple(TotalStockSummaryRow(
        group_name=row[group_field], item_code=row["item_id"],
        description=row["item__description"], current_qty=row["current_qty"],
    ) for row in aggregates if row["current_qty"] != 0)
    return TotalStockSummaryResult(rows, group_by)
