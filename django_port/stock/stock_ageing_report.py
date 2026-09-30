"""Stock age layers reconstructed from active ledger movements."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError

from catalog.models import Brand, Item
from organizations.models import Company

from .models import StockLedgerEntry, Warehouse, WarehouseType


ZERO = Decimal("0")
PRECISION = Decimal("0.000000001")


@dataclass
class AgeLayer:
    quantity: Decimal
    received_on: date
    value: Decimal


@dataclass(frozen=True)
class StockAgeingRow:
    item: Item
    warehouse: Warehouse | None
    stock_uom: str
    available_qty: Decimal
    average_age: Decimal
    earliest_age: int
    latest_age: int
    bucket_quantities: tuple[Decimal, ...]
    bucket_values: tuple[Decimal, ...]

    @property
    def bucket_pairs(self):
        return tuple(zip(self.bucket_quantities, self.bucket_values))


@dataclass(frozen=True)
class StockAgeingChartBar:
    item_code: str
    average_age: Decimal
    width_percent: Decimal


@dataclass(frozen=True)
class StockAgeingResult:
    rows: tuple[StockAgeingRow, ...]
    bucket_labels: tuple[str, ...]
    currency: str
    show_warehouse_wise_stock: bool

    @property
    def chart_bars(self):
        if self.show_warehouse_wise_stock:
            return ()
        oldest = sorted(self.rows, key=lambda row: (-row.average_age, row.item.pk))[:10]
        max_age = oldest[0].average_age if oldest else ZERO
        return tuple(StockAgeingChartBar(
            row.item.pk, row.average_age,
            (row.average_age * 100 / max_age).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if max_age else ZERO,
        ) for row in oldest)


def _ranges(value):
    try:
        ranges = tuple(int(part.strip()) for part in value.split(",")) if isinstance(value, str) else tuple(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Age ranges must be increasing whole numbers separated by commas.") from exc
    if not ranges or any(type(bound) is not int or bound < 0 for bound in ranges):
        raise ValidationError("Age ranges must be nonnegative whole numbers.")
    if any(left >= right for left, right in zip(ranges, ranges[1:])):
        raise ValidationError("Age ranges must increase strictly.")
    return ranges


def _consume(layers, quantity, *, lifo):
    remaining = -quantity
    consumed = []
    while remaining > ZERO:
        if not layers:
            raise ValidationError("Stock ageing cannot reconstruct a negative stock balance.")
        layer = layers[-1] if lifo else layers[0]
        taken = min(remaining, layer.quantity)
        original_qty = layer.quantity
        taken_value = layer.value * taken / original_qty
        consumed.append(AgeLayer(taken, layer.received_on, taken_value))
        layer.quantity -= taken
        layer.value -= taken_value
        remaining -= taken
        if layer.quantity == ZERO:
            layers.pop(-1 if lifo else 0)
    if lifo:
        consumed.reverse()
    return consumed


def stock_ageing_report(*, company, to_date, item=None, warehouse=None, warehouse_type=None,
                        brand=None, age_ranges="30,60,90", show_warehouse_wise_stock=False):
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if not isinstance(to_date, date):
        raise ValidationError("Select a valid stock ageing date.")
    if item is not None and not isinstance(item, Item):
        raise TypeError("item must be an Item instance")
    if brand is not None and not isinstance(brand, Brand):
        raise TypeError("brand must be a Brand instance")
    if warehouse_type is not None and not isinstance(warehouse_type, WarehouseType):
        raise TypeError("warehouse_type must be a WarehouseType instance")
    if warehouse is not None:
        if not isinstance(warehouse, Warehouse):
            raise TypeError("warehouse must be a Warehouse instance")
        if warehouse.company_id != company.pk:
            raise ValidationError("Warehouse must belong to the selected company.")
        warehouse = Warehouse.objects.get(pk=warehouse.pk)
    ranges = _ranges(age_ranges)
    labels = tuple(
        f"{ranges[index - 1] + 1 if index else 0}-{bound}"
        for index, bound in enumerate(ranges)
    ) + (f"{ranges[-1] + 1}+",)

    entries = StockLedgerEntry.objects.filter(
        company=company, is_cancelled=False, posting_date__lte=to_date,
    )
    if item is not None:
        entries = entries.filter(item=item)
    if brand is not None:
        entries = entries.filter(item__brand=brand)
    queues = {}
    identities = {}
    outgoing_by_detail = {}
    lifo = company.valuation_method == Company.ValuationMethod.LIFO
    moving_average = company.valuation_method == Company.ValuationMethod.MOVING_AVERAGE
    for entry in entries.select_related("item", "warehouse").order_by(
        "posting_datetime", "creation", "name"
    ):
        key = (entry.item_id, entry.warehouse_id)
        layers = queues.setdefault(key, [])
        identities[key] = (entry.item, entry.warehouse, entry.stock_uom_id)
        if entry.actual_qty > ZERO:
            source_detail = entry.dependant_sle_voucher_detail_no
            if source_detail:
                transfer_key = (entry.voucher_type, entry.voucher_no, entry.item_id,
                                source_detail)
                transferred = outgoing_by_detail.pop(transfer_key, None)
                if transferred is None or sum((part.quantity for part in transferred), ZERO) != entry.actual_qty:
                    raise ValidationError("Stock ageing cannot match this transfer receipt to its source.")
                layers.extend(transferred)
            else:
                layers.append(AgeLayer(entry.actual_qty, entry.posting_date,
                                       entry.stock_value_difference))
        elif entry.actual_qty < ZERO:
            consumed = _consume(layers, entry.actual_qty, lifo=lifo)
            if entry.voucher_detail_no:
                transfer_key = (entry.voucher_type, entry.voucher_no, entry.item_id,
                                entry.voucher_detail_no)
                outgoing_by_detail[transfer_key] = consumed
        if layers:
            total_qty = sum((layer.quantity for layer in layers), ZERO)
            if total_qty != entry.qty_after_transaction:
                raise ValidationError("Stock ageing layers do not match the stock ledger balance.")
            # Revaluation and moving average affect value without changing receipt dates.
            if moving_average or entry.is_value_reset:
                for layer in layers:
                    layer.value = entry.stock_value * layer.quantity / total_qty
            else:
                difference = entry.stock_value - sum((layer.value for layer in layers), ZERO)
                if difference:
                    layers[-1].value += difference
        elif entry.qty_after_transaction != ZERO:
            raise ValidationError("Stock ageing layers do not match the stock ledger balance.")

    grouped = {}
    for key, layers in queues.items():
        if not layers:
            continue
        item_obj, warehouse_obj, stock_uom = identities[key]
        if warehouse is not None and not (warehouse.lft <= warehouse_obj.lft
                                          and warehouse_obj.rgt <= warehouse.rgt):
            continue
        if warehouse_type is not None and warehouse_obj.warehouse_type_id != warehouse_type.pk:
            continue
        group_key = key if show_warehouse_wise_stock else (key[0], None)
        group = grouped.setdefault(group_key, (item_obj,
                                                warehouse_obj if show_warehouse_wise_stock else None,
                                                stock_uom, []))
        group[3].extend(layers)

    rows = []
    for item_obj, warehouse_obj, stock_uom, layers in grouped.values():
        total_qty = sum((layer.quantity for layer in layers), ZERO)
        bucket_qty = [ZERO] * (len(ranges) + 1)
        bucket_value = [ZERO] * (len(ranges) + 1)
        ages = []
        for layer in layers:
            age = (to_date - layer.received_on).days
            ages.append(age)
            index = next((i for i, bound in enumerate(ranges) if age <= bound), len(ranges))
            bucket_qty[index] += layer.quantity
            bucket_value[index] += layer.value
        average = (sum((Decimal(age) * layer.quantity for age, layer in zip(ages, layers)), ZERO)
                   / total_qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows.append(StockAgeingRow(item_obj, warehouse_obj, stock_uom, total_qty,
                                   average, max(ages), min(ages), tuple(bucket_qty),
                                   tuple(value.quantize(PRECISION, rounding=ROUND_HALF_UP)
                                         for value in bucket_value)))
    rows.sort(key=lambda row: (row.item.pk, row.warehouse.pk if row.warehouse else ""))
    return StockAgeingResult(tuple(rows), labels, company.default_currency_id,
                             bool(show_warehouse_wise_stock))
