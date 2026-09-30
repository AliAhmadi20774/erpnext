"""Company-wide first stock-ledger or Bin discrepancy per item and warehouse."""

from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError

from catalog.models import Item
from organizations.models import Company

from .models import Bin, Warehouse
from .stock_invariant_report import stock_invariant_report


DIFFERENCE_TYPES = {"All", "Qty", "Value", "Valuation"}


@dataclass(frozen=True)
class StockVarianceRow:
    item: Item
    warehouse: Warehouse
    valuation_method: str
    source: str
    entry_name: str
    posting_date: object
    qty_difference: Decimal | None
    value_difference: Decimal | None
    rate_difference: Decimal | None
    queue_qty_difference: Decimal | None
    queue_value_difference: Decimal | None
    queue_rate_difference: Decimal | None
    queue_error: bool

    def matches(self, difference_in):
        quantity = bool(self.qty_difference or self.queue_qty_difference)
        value = bool(self.value_difference or self.queue_value_difference or self.queue_error)
        valuation = bool(self.rate_difference or self.queue_rate_difference)
        return {
            "All": quantity or value or valuation,
            "Qty": quantity,
            "Value": value,
            "Valuation": valuation,
        }[difference_in]


@dataclass(frozen=True)
class StockVarianceResult:
    rows: tuple[StockVarianceRow, ...]
    currency: str


def stock_variance_report(*, company, item=None, warehouse=None,
                          difference_in="All", include_disabled=False):
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if item is not None and not isinstance(item, Item):
        raise TypeError("item must be an Item instance")
    if warehouse is not None:
        if not isinstance(warehouse, Warehouse):
            raise TypeError("warehouse must be a Warehouse instance")
        if warehouse.company_id != company.pk or warehouse.is_group:
            raise ValidationError("Select a leaf warehouse from the selected company.")
    if difference_in not in DIFFERENCE_TYPES:
        raise ValidationError("Select a valid difference type.")

    bins = Bin.objects.filter(company=company, item__is_stock_item=True,
                              warehouse__is_group=False).select_related("item", "warehouse")
    if item is not None:
        bins = bins.filter(item=item)
    if warehouse is not None:
        bins = bins.filter(warehouse=warehouse)
    if not include_disabled:
        bins = bins.filter(item__disabled=False, warehouse__disabled=False)

    rows = []
    for item_bin in bins.order_by("item_id", "warehouse_id"):
        report = stock_invariant_report(
            company=company, item=item_bin.item, warehouse=item_bin.warehouse,
        )
        first = None
        for check in report.rows:
            candidate = StockVarianceRow(
                item=item_bin.item, warehouse=item_bin.warehouse,
                valuation_method=company.valuation_method, source="Stock Ledger Entry",
                entry_name=check.entry.pk, posting_date=check.entry.posting_date,
                qty_difference=check.qty_difference, value_difference=check.value_difference,
                rate_difference=check.rate_difference,
                queue_qty_difference=check.queue_qty_difference,
                queue_value_difference=check.queue_value_difference,
                queue_rate_difference=check.queue_rate_difference,
                queue_error=check.queue_error,
            )
            if candidate.matches(difference_in):
                first = candidate
                break
        if first is None:
            check = report.bin_check
            candidate = StockVarianceRow(
                item=item_bin.item, warehouse=item_bin.warehouse,
                valuation_method=company.valuation_method, source="Bin",
                entry_name="", posting_date=None,
                qty_difference=check.qty_difference,
                value_difference=check.value_difference,
                rate_difference=check.rate_difference,
                queue_qty_difference=None, queue_value_difference=None,
                queue_rate_difference=None,
                queue_error=False,
            )
            if candidate.matches(difference_in):
                first = candidate
        if first is not None:
            rows.append(first)
    return StockVarianceResult(tuple(rows), company.default_currency_id)
