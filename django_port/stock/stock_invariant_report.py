"""Read-only consistency check for one item and warehouse ledger."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError

from catalog.models import Item
from organizations.models import Company

from .models import Bin, StockLedgerEntry, Warehouse


ZERO = Decimal("0")
PRECISION = Decimal("0.000000001")


@dataclass(frozen=True)
class StockInvariantRow:
    entry: StockLedgerEntry
    expected_qty: Decimal
    expected_value: Decimal
    qty_difference: Decimal
    value_difference: Decimal
    rate_difference: Decimal | None
    queue_qty_difference: Decimal | None
    queue_value_difference: Decimal | None
    queue_rate_difference: Decimal | None
    queue_error: bool

    @property
    def has_issue(self):
        return any((self.qty_difference, self.value_difference,
                    self.rate_difference, self.queue_qty_difference,
                    self.queue_value_difference, self.queue_rate_difference,
                    self.queue_error))


@dataclass(frozen=True)
class StockInvariantBin:
    bin: Bin | None
    has_history: bool
    expected_qty: Decimal
    expected_value: Decimal
    expected_rate: Decimal
    qty_difference: Decimal | None
    value_difference: Decimal | None
    rate_difference: Decimal | None

    @property
    def has_issue(self):
        if self.bin is None:
            return self.has_history
        return any((self.qty_difference, self.value_difference, self.rate_difference))


@dataclass(frozen=True)
class StockInvariantResult:
    rows: tuple[StockInvariantRow, ...]
    bin_check: StockInvariantBin
    has_issues: bool
    currency: str


def _rate(value, quantity):
    return (value / quantity).quantize(PRECISION, rounding=ROUND_HALF_UP) if quantity else ZERO


def _queue_balance(queue):
    if not isinstance(queue, list):
        return None
    quantity = value = ZERO
    try:
        for layer in queue:
            if not isinstance(layer, (list, tuple)) or len(layer) != 2:
                return None
            layer_qty, layer_rate = Decimal(str(layer[0])), Decimal(str(layer[1]))
            if not layer_qty.is_finite() or not layer_rate.is_finite() or layer_qty <= ZERO or layer_rate < ZERO:
                return None
            quantity += layer_qty
            value += layer_qty * layer_rate
    except (InvalidOperation, TypeError, ValueError):
        return None
    return quantity, value.quantize(PRECISION, rounding=ROUND_HALF_UP)


def stock_invariant_report(*, company, item, warehouse, show_incorrect_entries=False):
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if not isinstance(item, Item):
        raise TypeError("item must be an Item instance")
    if not isinstance(warehouse, Warehouse):
        raise TypeError("warehouse must be a Warehouse instance")
    if warehouse.company_id != company.pk or warehouse.is_group:
        raise ValidationError("Select a leaf warehouse from the selected company.")

    expected_qty = expected_value = ZERO
    all_rows = []
    latest_rate = ZERO
    entries = StockLedgerEntry.objects.filter(
        company=company, item=item, warehouse=warehouse, is_cancelled=False,
    ).order_by("posting_datetime", "creation", "name")
    for entry in entries:
        expected_qty += entry.actual_qty
        expected_value += entry.stock_value_difference
        latest_rate = entry.valuation_rate
        rate_difference = None
        if entry.qty_after_transaction and entry.stock_value:
            rate_difference = entry.valuation_rate - _rate(entry.stock_value, entry.qty_after_transaction)
        queue_qty_difference = queue_value_difference = queue_rate_difference = None
        queue_error = False
        if company.valuation_method != Company.ValuationMethod.MOVING_AVERAGE:
            queue_balance = _queue_balance(entry.stock_queue)
            if queue_balance is None:
                queue_error = True
            else:
                queue_qty_difference = entry.qty_after_transaction - queue_balance[0]
                queue_value_difference = entry.stock_value - queue_balance[1]
                if queue_balance[0]:
                    queue_rate_difference = entry.valuation_rate - _rate(queue_balance[1], queue_balance[0])
        all_rows.append(StockInvariantRow(
            entry=entry, expected_qty=expected_qty, expected_value=expected_value,
            qty_difference=entry.qty_after_transaction - expected_qty,
            value_difference=entry.stock_value - expected_value,
            rate_difference=rate_difference,
            queue_qty_difference=queue_qty_difference,
            queue_value_difference=queue_value_difference,
            queue_rate_difference=queue_rate_difference,
            queue_error=queue_error,
        ))

    item_bin = Bin.objects.filter(company=company, item=item, warehouse=warehouse).first()
    bin_check = StockInvariantBin(
        bin=item_bin, has_history=bool(all_rows),
        expected_qty=expected_qty, expected_value=expected_value,
        expected_rate=latest_rate,
        qty_difference=item_bin.actual_qty - expected_qty if item_bin else None,
        value_difference=item_bin.stock_value - expected_value if item_bin else None,
        rate_difference=item_bin.valuation_rate - latest_rate if item_bin else None,
    )
    if show_incorrect_entries:
        first_issue = next((index for index, row in enumerate(all_rows) if row.has_issue), None)
        rows = () if first_issue is None else tuple(all_rows[max(first_issue - 1, 0):])
    else:
        rows = tuple(all_rows)
    return StockInvariantResult(
        rows=rows, bin_check=bin_check,
        has_issues=bin_check.has_issue or any(row.has_issue for row in all_rows),
        currency=company.default_currency_id,
    )
