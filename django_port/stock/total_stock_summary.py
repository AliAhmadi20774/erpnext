"""Current or historical stock grouped by company or warehouse and item."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum

from organizations.models import Company

from .models import Bin, StockLedgerEntry


@dataclass(frozen=True)
class TotalStockSummaryRow:
    group_name: str
    item_code: str
    description: str
    current_qty: Decimal
    stock_value: Decimal
    currency: str


@dataclass(frozen=True)
class TotalStockSummaryResult:
    rows: tuple[TotalStockSummaryRow, ...]
    group_by: str
    as_on_date: date | None


def total_stock_summary(*, group_by="Warehouse", company=None, as_on_date=None):
    if group_by not in {"Warehouse", "Company"}:
        raise ValidationError("Group by must be Warehouse or Company.")
    if company is not None and not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if as_on_date is not None and not isinstance(as_on_date, date):
        raise ValidationError("Select a valid as-of date.")
    if group_by == "Warehouse" and company is None:
        raise ValidationError("Select a company for warehouse grouping.")

    group_field = "warehouse_id" if group_by == "Warehouse" else "company_id"
    if as_on_date is None:
        source = Bin.objects.exclude(actual_qty=0)
        quantity_field, value_field = "actual_qty", "stock_value"
    else:
        source = StockLedgerEntry.objects.filter(
            is_cancelled=False, posting_date__lte=as_on_date,
        )
        quantity_field, value_field = "actual_qty", "stock_value_difference"
    if company is not None:
        source = source.filter(company=company)
    aggregates = source.values(
        group_field, "item_id", "item__description", "company__default_currency_id",
    ).annotate(
        current_qty=Sum(quantity_field), stock_value=Sum(value_field),
    ).order_by(group_field, "item_id")
    rows = tuple(TotalStockSummaryRow(
        group_name=row[group_field], item_code=row["item_id"],
        description=row["item__description"], current_qty=row["current_qty"],
        stock_value=row["stock_value"], currency=row["company__default_currency_id"],
    ) for row in aggregates if row["current_qty"] != 0)
    return TotalStockSummaryResult(rows, group_by, as_on_date)
