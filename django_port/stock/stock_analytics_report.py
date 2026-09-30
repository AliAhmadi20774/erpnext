"""Periodic quantity or value balances from active stock ledger entries."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError

from accounting.fiscal import resolve_fiscal_year
from catalog.models import Brand, Item, ItemGroup
from organizations.models import Company

from .models import StockLedgerEntry, Warehouse, WarehouseType


ZERO = Decimal("0")
FREQUENCIES = ("Weekly", "Monthly", "Quarterly", "Yearly")
MEASURES = ("Quantity", "Value")


@dataclass(frozen=True)
class StockAnalyticsPeriod:
    start: date
    end: date
    label: str


@dataclass(frozen=True)
class StockAnalyticsRow:
    item: Item
    balances: tuple[Decimal, ...]


@dataclass(frozen=True)
class StockAnalyticsResult:
    periods: tuple[StockAnalyticsPeriod, ...]
    rows: tuple[StockAnalyticsRow, ...]
    measure: str
    currency: str

    @property
    def chart_data(self):
        return {
            "labels": [period.label for period in self.periods],
            "series": [
                {"item": row.item.pk, "balances": [str(balance) for balance in row.balances]}
                for row in self.rows
            ],
        }


def _month_start(value):
    return value.replace(day=1)


def _add_months(value, count):
    month_index = value.year * 12 + value.month - 1 + count
    return date(month_index // 12, month_index % 12 + 1, 1)


def _periods(company, from_date, to_date, frequency):
    if frequency == "Weekly":
        cursor = from_date - timedelta(days=from_date.weekday())
    elif frequency == "Monthly":
        cursor = _month_start(from_date)
    elif frequency == "Quarterly":
        cursor = date(from_date.year, (from_date.month - 1) // 3 * 3 + 1, 1)
    else:
        cursor = resolve_fiscal_year(from_date, company).year_start_date
    periods = []
    while cursor <= to_date:
        if len(periods) >= 52:
            raise ValidationError("Select a date range with at most 52 reporting periods.")
        if frequency == "Weekly":
            natural_end = cursor + timedelta(days=6)
        elif frequency == "Monthly":
            natural_end = _add_months(cursor, 1) - timedelta(days=1)
        elif frequency == "Quarterly":
            natural_end = _add_months(cursor, 3) - timedelta(days=1)
        else:
            year = resolve_fiscal_year(cursor, company)
            if year.year_start_date != cursor:
                raise ValidationError("Reporting fiscal years must be consecutive.")
            natural_end = year.year_end_date
        end = min(natural_end, to_date)
        if frequency == "Weekly":
            label = f"Week {end.isocalendar().week} {end.year}"
        elif frequency == "Monthly":
            label = end.strftime("%b %Y")
        elif frequency == "Quarterly":
            label = f"Quarter {(end.month - 1) // 3 + 1} {end.year}"
        else:
            label = year.year
        periods.append(StockAnalyticsPeriod(cursor, end, label))
        cursor = end + timedelta(days=1)
    return tuple(periods)


def stock_analytics_report(*, company, from_date, to_date, frequency="Monthly",
                           measure="Value", item=None, item_group=None, brand=None,
                           warehouse=None, warehouse_type=None):
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if not isinstance(from_date, date) or not isinstance(to_date, date) or from_date > to_date:
        raise ValidationError("Select a valid stock analytics date range.")
    if frequency not in FREQUENCIES or measure not in MEASURES:
        raise ValidationError("Select a valid frequency and balance measure.")
    if item is not None and not isinstance(item, Item):
        raise TypeError("item must be an Item instance")
    if item_group is not None:
        if not isinstance(item_group, ItemGroup):
            raise TypeError("item_group must be an ItemGroup instance")
        item_group = ItemGroup.objects.get(pk=item_group.pk)
    if brand is not None and not isinstance(brand, Brand):
        raise TypeError("brand must be a Brand instance")
    if warehouse is not None:
        if not isinstance(warehouse, Warehouse):
            raise TypeError("warehouse must be a Warehouse instance")
        if warehouse.company_id != company.pk:
            raise ValidationError("Warehouse must belong to the selected company.")
        warehouse = Warehouse.objects.get(pk=warehouse.pk)
    if warehouse_type is not None and not isinstance(warehouse_type, WarehouseType):
        raise TypeError("warehouse_type must be a WarehouseType instance")
    periods = _periods(company, from_date, to_date, frequency)

    items = Item.objects.filter(is_stock_item=True).select_related("item_group", "brand", "stock_uom")
    if item is not None:
        items = items.filter(pk=item.pk)
    if item_group is not None:
        items = items.filter(item_group__lft__gte=item_group.lft,
                             item_group__rgt__lte=item_group.rgt)
    if brand is not None:
        items = items.filter(brand=brand)
    item_list = list(items.order_by("name"))
    balances = {item_obj.pk: ZERO for item_obj in item_list}
    snapshots = {item_obj.pk: [] for item_obj in item_list}

    entries = StockLedgerEntry.objects.filter(
        company=company, item_id__in=balances, is_cancelled=False,
        posting_date__lte=to_date,
    )
    if warehouse is not None:
        entries = entries.filter(warehouse__lft__gte=warehouse.lft,
                                 warehouse__rgt__lte=warehouse.rgt)
    elif warehouse_type is not None:
        entries = entries.filter(warehouse__warehouse_type=warehouse_type)
    movements = iter(entries.order_by("posting_datetime", "creation", "name").values_list(
        "item_id", "posting_date", "actual_qty" if measure == "Quantity" else "stock_value_difference",
    ))
    current = next(movements, None)
    for period in periods:
        while current is not None and current[1] <= period.end:
            balances[current[0]] += current[2]
            current = next(movements, None)
        for item_obj in item_list:
            snapshots[item_obj.pk].append(balances[item_obj.pk])
    rows = tuple(StockAnalyticsRow(item_obj, tuple(snapshots[item_obj.pk]))
                 for item_obj in item_list)
    return StockAnalyticsResult(periods, rows, measure, company.default_currency_id)
