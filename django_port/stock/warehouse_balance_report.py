"""Current stock value rolled up through a company's warehouse tree."""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from organizations.models import Company

from .models import StockLedgerEntry, Warehouse


ZERO = Decimal("0")


@dataclass
class WarehouseBalanceRow:
    warehouse: Warehouse
    indent: int
    stock_balance: Decimal = ZERO


@dataclass(frozen=True)
class WarehouseBalanceResult:
    rows: tuple[WarehouseBalanceRow, ...]
    currency: str


def warehouse_balance_report(*, company, show_disabled_warehouses=False):
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")

    balances = {
        row["warehouse_id"]: row["balance"] or ZERO
        for row in StockLedgerEntry.objects.filter(
            company=company, is_cancelled=False,
        ).values("warehouse_id").annotate(balance=Sum("stock_value_difference"))
    }
    warehouses = Warehouse.objects.filter(company=company)
    if not show_disabled_warehouses:
        warehouses = warehouses.filter(disabled=False)
    rows = []
    by_name = {}
    for warehouse in warehouses.order_by("lft", "name"):
        parent = by_name.get(warehouse.parent_warehouse_id)
        row = WarehouseBalanceRow(
            warehouse=warehouse, indent=parent.indent + 1 if parent else 0,
            stock_balance=balances.get(warehouse.pk, ZERO),
        )
        rows.append(row)
        by_name[warehouse.pk] = row
    for row in reversed(rows):
        parent = by_name.get(row.warehouse.parent_warehouse_id)
        if parent is not None:
            parent.stock_balance += row.stock_balance
    return WarehouseBalanceResult(tuple(rows), company.default_currency_id)
