from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounting.fiscal import resolve_fiscal_year
from catalog.models import Item
from organizations.models import Company
from projects.models import Project

from .models import Bin, StockLedgerEntry, Warehouse


PRECISION = Decimal("0.000000001")
ZERO = Decimal("0")
_STOCK_ENTRY_REPLAY_TOKEN = object()


def _decimal(value):
    try:
        return Decimal(str(value)).quantize(PRECISION, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValidationError("Stock quantities and rates must be valid numbers.") from error


@dataclass(frozen=True)
class StockLedgerLine:
    item: Item
    warehouse: Warehouse
    quantity: Decimal
    incoming_rate: Decimal | None = None
    project: Project | None = None
    voucher_detail_no: str = ""
    rate_from_voucher_detail_no: str = ""
    is_value_adjustment: bool = False


def _posting_datetime(posting_date, posting_time):
    value = datetime.combine(posting_date, posting_time)
    if timezone.is_naive(value) and timezone.is_aware(timezone.now()):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _normalise_queue(raw_queue):
    queue = []
    for layer in raw_queue or []:
        if not isinstance(layer, (list, tuple)) or len(layer) != 2:
            raise ValidationError("The last stock queue is invalid.")
        quantity, rate = (_decimal(layer[0]), _decimal(layer[1]))
        if quantity <= ZERO or rate < ZERO:
            raise ValidationError("The last stock queue contains an invalid layer.")
        queue.append([quantity, rate])
    return queue


def _serialise_queue(queue):
    return [[format(quantity, "f"), format(rate, "f")] for quantity, rate in queue]


def _queue_valuation(*, queue, quantity, incoming_rate, lifo, incoming_layers=None):
    queue = [layer.copy() for layer in queue]
    consumed_layers = []
    if quantity > ZERO:
        if incoming_layers is None:
            queue.append([quantity, incoming_rate])
        else:
            if sum((layer[0] for layer in incoming_layers), ZERO) != quantity:
                raise ValidationError("Transfer layers do not match the incoming quantity.")
            queue.extend(layer.copy() for layer in incoming_layers)
        outgoing_rate = ZERO
    else:
        remaining = -quantity
        consumed_value = ZERO
        while remaining > ZERO:
            if not queue:
                raise ValidationError("Insufficient stock queue for this outgoing quantity.")
            index = -1 if lifo else 0
            layer_quantity, layer_rate = queue[index]
            consumed = min(layer_quantity, remaining)
            consumed_value += consumed * layer_rate
            consumed_layers.append([consumed, layer_rate])
            layer_quantity -= consumed
            remaining -= consumed
            if layer_quantity == ZERO:
                queue.pop(index)
            else:
                queue[index][0] = layer_quantity
        outgoing_rate = _decimal(consumed_value / -quantity)
        if lifo:
            consumed_layers.reverse()

    stock_value = _decimal(sum((qty * rate for qty, rate in queue), ZERO))
    return queue, stock_value, outgoing_rate, consumed_layers


@transaction.atomic
def post_stock_entries(
    *, company, posting_date, posting_time, voucher_type, voucher_no, lines,
    _defer_replay=None,
):
    """Post stock rows; deferred valuation belongs to the stock replay service."""
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    voucher_type = (voucher_type or "").strip()
    voucher_no = (voucher_no or "").strip()
    if not voucher_type or not voucher_no:
        raise ValidationError("Voucher type and voucher number are required.")
    if _defer_replay is not None and _defer_replay is not _STOCK_ENTRY_REPLAY_TOKEN:
        raise ValidationError("Deferred valuation requires the Stock Entry replay service.")
    defer_replay = _defer_replay is _STOCK_ENTRY_REPLAY_TOKEN
    if defer_replay and voucher_type not in {"Stock Entry", "Stock Reconciliation"}:
        raise ValidationError("Deferred valuation is limited to supported stock vouchers.")

    lines = tuple(lines)
    if not lines:
        raise ValidationError("At least one stock line is required.")
    if any(not isinstance(line, StockLedgerLine) for line in lines):
        raise TypeError("lines must contain StockLedgerLine instances")

    company = Company.objects.select_for_update().get(pk=company.pk)
    if StockLedgerEntry.objects.filter(
        company=company, voucher_type=voucher_type, voucher_no=voucher_no
    ).exists():
        raise ValidationError("This voucher has already been posted to the stock ledger.")

    fiscal_year = resolve_fiscal_year(posting_date, company)
    posting_datetime = _posting_datetime(posting_date, posting_time)
    created = []
    created_by_detail = {}
    consumed_layers_by_detail = {}

    for line in lines:
        quantity = _decimal(line.quantity)
        if line.is_value_adjustment:
            if quantity != ZERO or voucher_type not in {"Stock Entry", "Stock Reconciliation"} or line.rate_from_voucher_detail_no:
                raise ValidationError("A value adjustment must be a zero-quantity stock voucher line.")
        elif quantity == ZERO:
            raise ValidationError("Stock line quantity must not be zero.")
        if not isinstance(line.item, Item) or not isinstance(line.warehouse, Warehouse):
            raise TypeError("Each line needs Item and Warehouse instances.")

        item = Item.objects.select_for_update().get(pk=line.item.pk)
        warehouse = Warehouse.objects.select_for_update().select_related(
            "company", "account", "parent_warehouse"
        ).get(pk=line.warehouse.pk)
        if warehouse.company_id != company.pk or warehouse.is_group or warehouse.disabled:
            raise ValidationError("Select an enabled leaf warehouse from the voucher company.")
        if item.disabled or not item.is_stock_item:
            raise ValidationError("Select an enabled stock item.")
        if not item.stock_uom.enabled:
            raise ValidationError("The item's stock UOM must be enabled.")
        if item.stock_uom.must_be_whole_number and quantity != quantity.to_integral_value():
            raise ValidationError("Stock quantity must be a whole number for this UOM.")
        if company.enable_perpetual_inventory:
            warehouse.effective_account()

        project = line.project
        if project is not None:
            if not isinstance(project, Project):
                raise TypeError("project must be a Project instance")
            project = Project.objects.get(pk=project.pk)
            if project.company_id != company.pk:
                raise ValidationError("Project must belong to the voucher company.")

        latest = StockLedgerEntry.objects.filter(
            item=item, warehouse=warehouse, is_cancelled=False
        ).order_by("-posting_datetime", "-creation", "-name").first()
        if latest and latest.posting_datetime > posting_datetime and not defer_replay:
            raise ValidationError(
                "Backdated stock posting requires the stock-ledger replay workflow."
            )

        item_bin = Bin.objects.select_for_update().filter(
            item=item, warehouse=warehouse
        ).first()
        if item_bin is None:
            item_bin = Bin(
                item=item,
                warehouse=warehouse,
                company=company,
                stock_uom=item.stock_uom,
            )
            item_bin.save(_allow_stock_write=True)

        old_quantity = _decimal(item_bin.actual_qty)
        old_value = _decimal(item_bin.stock_value)
        new_quantity = _decimal(old_quantity + quantity)
        if new_quantity < ZERO and not defer_replay:
            raise ValidationError(
                f"Insufficient stock for {item.pk} in {warehouse.pk}."
            )
        if line.is_value_adjustment and old_quantity <= ZERO and not defer_replay:
            raise ValidationError("A direct value adjustment requires stock on hand.")

        incoming_rate = ZERO
        outgoing_rate = ZERO
        stock_queue = []
        transfer_source = None
        if quantity > ZERO or line.is_value_adjustment:
            if line.rate_from_voucher_detail_no:
                transfer_source = created_by_detail.get(line.rate_from_voucher_detail_no)
                if (
                    transfer_source is None
                    or transfer_source.actual_qty >= ZERO
                    or transfer_source.item_id != item.pk
                    or -transfer_source.actual_qty != quantity
                    or line.incoming_rate is not None
                ):
                    raise ValidationError(
                        "A transfer receipt must match an earlier outgoing voucher row."
                    )
                incoming_rate = _decimal(transfer_source.outgoing_rate)
            elif line.incoming_rate is None:
                raise ValidationError("Incoming stock requires an incoming rate.")
            else:
                incoming_rate = _decimal(line.incoming_rate)
            if incoming_rate < ZERO:
                raise ValidationError("Incoming stock rate cannot be negative.")
        elif line.incoming_rate not in (None, ZERO, 0, "0"):
            raise ValidationError("Outgoing stock cannot specify an incoming rate.")

        if defer_replay:
            # A staging row has no reliable as-of balance yet. The caller must
            # replay the complete company ledger before committing this transaction.
            new_quantity = ZERO
            new_value = ZERO
            valuation_rate = ZERO
        elif line.is_value_adjustment:
            if company.valuation_method != Company.ValuationMethod.MOVING_AVERAGE:
                queue = _normalise_queue(latest.stock_queue if latest else [])
                if (
                    _decimal(sum((layer[0] for layer in queue), ZERO)) != old_quantity
                    or _decimal(sum((qty * rate for qty, rate in queue), ZERO)) != old_value
                ):
                    raise ValidationError("Stock queue and Bin are inconsistent; rebuild the stock balance first.")
                stock_queue = _serialise_queue([[old_quantity, incoming_rate]])
            new_value = _decimal(old_quantity * incoming_rate)
            valuation_rate = incoming_rate
        elif company.valuation_method == Company.ValuationMethod.MOVING_AVERAGE:
            if quantity > ZERO:
                new_value = _decimal(old_value + quantity * incoming_rate)
            else:
                outgoing_rate = _decimal(item_bin.valuation_rate)
                new_value = _decimal(old_value + quantity * outgoing_rate)
            if new_quantity == ZERO:
                new_value = ZERO
            valuation_rate = (
                _decimal(new_value / new_quantity) if new_quantity else ZERO
            )
        else:
            queue = _normalise_queue(latest.stock_queue if latest else [])
            queue_quantity = _decimal(sum((layer[0] for layer in queue), ZERO))
            queue_value = _decimal(sum((qty * rate for qty, rate in queue), ZERO))
            if queue_quantity != old_quantity or queue_value != old_value:
                raise ValidationError(
                    "Stock queue and Bin are inconsistent; rebuild the stock balance first."
                )
            incoming_layers = (
                consumed_layers_by_detail.get(line.rate_from_voucher_detail_no)
                if transfer_source
                else None
            )
            queue, new_value, outgoing_rate, consumed_layers = _queue_valuation(
                queue=queue,
                quantity=quantity,
                incoming_rate=incoming_rate,
                lifo=company.valuation_method == Company.ValuationMethod.LIFO,
                incoming_layers=incoming_layers,
            )
            stock_queue = _serialise_queue(queue)
            valuation_rate = (
                _decimal(new_value / new_quantity) if new_quantity else ZERO
            )

        value_difference = ZERO if defer_replay else _decimal(new_value - old_value)
        if not defer_replay and transfer_source and value_difference != -transfer_source.stock_value_difference:
            raise ValidationError(
                "Transfer valuation does not balance; split the stock line or repost valuation."
            )
        entry = StockLedgerEntry(
            item=item,
            warehouse=warehouse,
            item_bin=item_bin,
            company=company,
            stock_uom=item.stock_uom,
            fiscal_year=fiscal_year,
            project=project,
            posting_date=posting_date,
            posting_time=posting_time,
            posting_datetime=posting_datetime,
            voucher_type=voucher_type,
            voucher_no=voucher_no,
            voucher_detail_no=(line.voucher_detail_no or "").strip(),
            actual_qty=quantity,
            qty_after_transaction=new_quantity,
            incoming_rate=incoming_rate,
            outgoing_rate=outgoing_rate,
            valuation_rate=valuation_rate,
            stock_value=new_value,
            stock_value_difference=value_difference,
            stock_queue=stock_queue,
            dependant_sle_voucher_detail_no=(
                line.rate_from_voucher_detail_no or ""
            ).strip(),
            is_value_reset=line.is_value_adjustment,
        )
        entry.save(_allow_stock_write=True)

        if not defer_replay:
            item_bin.actual_qty = new_quantity
            item_bin.valuation_rate = valuation_rate
            item_bin.stock_value = new_value
            item_bin.save(_allow_stock_write=True)
        created.append(entry)
        detail_no = (line.voucher_detail_no or "").strip()
        if detail_no:
            if detail_no in created_by_detail:
                raise ValidationError("Voucher detail identifiers must be unique.")
            created_by_detail[detail_no] = entry
            if quantity < ZERO and not defer_replay and company.valuation_method != Company.ValuationMethod.MOVING_AVERAGE:
                consumed_layers_by_detail[detail_no] = consumed_layers

    return tuple(created)
