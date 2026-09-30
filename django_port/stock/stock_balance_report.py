"""Historical stock balances derived from active ledger entries."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError

from catalog.models import Item, ItemGroup
from organizations.models import Company

from .models import StockLedgerEntry, Warehouse, WarehouseType


ZERO = Decimal("0")


@dataclass
class StockBalanceRow:
    item: Item
    warehouse: Warehouse
    stock_uom: str
    opening_qty: Decimal = ZERO
    opening_value: Decimal = ZERO
    in_qty: Decimal = ZERO
    in_value: Decimal = ZERO
    out_qty: Decimal = ZERO
    out_value: Decimal = ZERO
    balance_qty: Decimal = ZERO
    balance_value: Decimal = ZERO
    valuation_rate: Decimal = ZERO


@dataclass(frozen=True)
class StockBalanceReportResult:
    rows: tuple[StockBalanceRow, ...]
    currency: str


def stock_balance_report(*, company, from_date, to_date, item=None, item_group=None,
                         warehouse=None, warehouse_type=None, include_zero_stock=False):
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if not isinstance(from_date, date) or not isinstance(to_date, date) or from_date > to_date:
        raise ValidationError("Select a valid stock balance date range.")
    if item is not None and not isinstance(item, Item):
        raise TypeError("item must be an Item instance")
    if item_group is not None:
        if not isinstance(item_group, ItemGroup):
            raise TypeError("item_group must be an ItemGroup instance")
        item_group = ItemGroup.objects.get(pk=item_group.pk)
    if warehouse is not None:
        if not isinstance(warehouse, Warehouse):
            raise TypeError("warehouse must be a Warehouse instance")
        if warehouse.company_id != company.pk:
            raise ValidationError("Warehouse must belong to the selected company.")
        warehouse = Warehouse.objects.get(pk=warehouse.pk)
    if warehouse_type is not None and not isinstance(warehouse_type, WarehouseType):
        raise TypeError("warehouse_type must be a WarehouseType instance")

    entries = StockLedgerEntry.objects.filter(
        company=company, is_cancelled=False, posting_date__lte=to_date,
    )
    if item is not None:
        entries = entries.filter(item=item)
    if item_group is not None:
        entries = entries.filter(item__item_group__lft__gte=item_group.lft,
                                 item__item_group__rgt__lte=item_group.rgt)
    if warehouse is not None:
        entries = entries.filter(warehouse__lft__gte=warehouse.lft,
                                 warehouse__rgt__lte=warehouse.rgt)
    if warehouse_type is not None:
        entries = entries.filter(warehouse__warehouse_type=warehouse_type)

    balances = {}
    for entry in entries.select_related("item", "item__item_group", "warehouse", "stock_uom").order_by(
        "posting_datetime", "creation", "name"
    ):
        key = (entry.item_id, entry.warehouse_id)
        row = balances.get(key)
        if row is None:
            row = StockBalanceRow(entry.item, entry.warehouse, entry.stock_uom_id)
            balances[key] = row
        qty_delta = entry.actual_qty
        value_delta = entry.stock_value_difference
        if entry.posting_date < from_date:
            row.opening_qty += qty_delta
            row.opening_value += value_delta
        else:
            if qty_delta >= ZERO:
                row.in_qty += qty_delta
            else:
                row.out_qty -= qty_delta
            if value_delta >= ZERO:
                row.in_value += value_delta
            else:
                row.out_value -= value_delta
        row.balance_qty += qty_delta
        row.balance_value += value_delta
        row.valuation_rate = entry.valuation_rate

    rows = tuple(
        row for row in sorted(balances.values(), key=lambda row: (row.item.pk, row.warehouse.pk))
        if include_zero_stock or row.balance_qty != ZERO or row.balance_value != ZERO
    )
    return StockBalanceReportResult(rows, company.default_currency_id)
