"""Stock value rolled up through a company's warehouse tree."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum

from catalog.models import Item
from organizations.models import Company

from .models import StockLedgerEntry, Warehouse


ZERO = Decimal("0")


@dataclass
class WarehouseBalanceRow:
    warehouse: Warehouse
    indent: int
    stock_balance: Decimal = ZERO
    stock_qty: Decimal | None = None


@dataclass(frozen=True)
class WarehouseBalanceResult:
    rows: tuple[WarehouseBalanceRow, ...]
    currency: str
    as_on_date: date | None
    stock_uom: str | None


def warehouse_balance_report(*, company, show_disabled_warehouses=False,
                             as_on_date=None, item=None):
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if as_on_date is not None and not isinstance(as_on_date, date):
        raise ValidationError("Select a valid as-of date.")
    if item is not None and not isinstance(item, Item):
        raise TypeError("item must be an Item instance")

    entries = StockLedgerEntry.objects.filter(company=company, is_cancelled=False)
    if as_on_date is not None:
        entries = entries.filter(posting_date__lte=as_on_date)
    if item is not None:
        entries = entries.filter(item=item)
    aggregates = {"balance": Sum("stock_value_difference")}
    if item is not None:
        aggregates["qty"] = Sum("actual_qty")
    balances = {
        row["warehouse_id"]: row
        for row in entries.values("warehouse_id").annotate(**aggregates)
    }
    warehouses = Warehouse.objects.filter(company=company)
    if not show_disabled_warehouses:
        warehouses = warehouses.filter(disabled=False)
    rows = []
    by_name = {}
    for warehouse in warehouses.order_by("lft", "name"):
        parent = by_name.get(warehouse.parent_warehouse_id)
        balance = balances.get(warehouse.pk)
        row = WarehouseBalanceRow(
            warehouse=warehouse, indent=parent.indent + 1 if parent else 0,
            stock_balance=balance["balance"] if balance else ZERO,
            stock_qty=(balance["qty"] if balance else ZERO) if item else None,
        )
        rows.append(row)
        by_name[warehouse.pk] = row
    for row in reversed(rows):
        parent = by_name.get(row.warehouse.parent_warehouse_id)
        if parent is not None:
            parent.stock_balance += row.stock_balance
            if item is not None:
                parent.stock_qty += row.stock_qty
    return WarehouseBalanceResult(
        tuple(rows), company.default_currency_id, as_on_date,
        item.stock_uom_id if item else None,
    )
