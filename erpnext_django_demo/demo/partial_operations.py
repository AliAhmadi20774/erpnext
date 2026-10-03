from collections import Counter
from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .accounting import post_fulfillment, post_fulfillment_batch, post_production_batch
from .models import (Fulfillment, FulfillmentBatch, FulfillmentBatchLine, Item, Order,
                     ProductionBatch, ProductionBatchMaterial, StockMovement, WorkOrder)
from .services import record_audit


@transaction.atomic
def fulfill_partial(order_id, quantities, actor=None, request_key=None, legacy_full=False):
    order = Order.objects.select_for_update().get(pk=order_id)
    if request_key:
        existing = FulfillmentBatch.objects.filter(request_key=request_key).first()
        if existing:
            actual = {row.order_line_id: row.quantity for row in existing.lines.all()}
            if existing.order_id != order.pk or actual != quantities:
                raise ValidationError("شناسهٔ درخواست نوبت تحویل با محتوای دیگری استفاده شده است.")
            return existing
    if order.status != Order.CONFIRMED or hasattr(order, "fulfillment"):
        raise ValidationError("سفارش باید تاییدشده و دارای مقدار تحویل‌نشده باشد.")
    from .approvals import ensure_purchase_approved
    ensure_purchase_approved(order)
    lines = {line.pk: line for line in order.lines.select_related("item").order_by("pk")}
    if not quantities or any(pk not in lines or type(qty) is not int or qty <= 0
                             or qty > lines[pk].remaining_quantity for pk, qty in quantities.items()):
        raise ValidationError("مقدار هر قلم باید مثبت و حداکثر برابر باقیماندهٔ همان سفارش باشد.")
    totals = Counter()
    for pk, qty in quantities.items():
        totals[lines[pk].item_id] += qty
    items = {item.pk: item for item in Item.objects.select_for_update().filter(pk__in=totals).order_by("pk")}
    if order.kind == Order.SALES and any(items[pk].stock < qty for pk, qty in totals.items()):
        raise ValidationError("موجودی برای این نوبت تحویل کافی نیست.")
    legacy = legacy_full and not order.fulfillment_batches.exists()
    batch = FulfillmentBatch.objects.create(order=order, legacy=legacy,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        **({"request_key": request_key} if request_key else {}))
    for pk, qty in quantities.items():
        line, item = lines[pk], items[lines[pk].item_id]
        FulfillmentBatchLine.objects.create(batch=batch, order_line=line, quantity=qty,
            unit_price=line.unit_price,
            unit_cost=item.purchase_price if order.kind == Order.SALES else line.unit_price)
    for pk, qty in totals.items():
        item = items[pk]
        before = item.stock
        item.stock += qty * (-1 if order.kind == Order.SALES else 1)
        item.save(update_fields=["stock"])
        StockMovement.objects.create(item=item, order=order, fulfillment_batch=batch,
            source=StockMovement.SALES if order.kind == Order.SALES else StockMovement.PURCHASE,
            change=item.stock - before, balance_before=before, balance_after=item.stock,
            note=f"نوبت {batch.number}")
    if all(line.remaining_quantity == 0 for line in lines.values()):
        marker = Fulfillment.objects.create(order=order)
    else:
        marker = None
    if legacy and marker:
        post_fulfillment(marker, actor)
    else:
        post_fulfillment_batch(batch, actor)
    record_audit(actor, "order_fulfilled" if legacy else "order_partially_fulfilled", order,
                 order.number, {"batch_id": batch.pk, "quantities": quantities})
    return batch


@transaction.atomic
def produce_partial(work_order_id, accepted, rejected=0, reason="", actor=None, request_key=None):
    work = WorkOrder.objects.select_for_update().select_related("bom__product").get(pk=work_order_id)
    if request_key:
        existing = ProductionBatch.objects.filter(request_key=request_key).first()
        if existing:
            if (existing.work_order_id != work.pk or existing.accepted_quantity != accepted
                    or existing.rejected_quantity != rejected or existing.quality_reason != reason.strip()):
                raise ValidationError("شناسهٔ نوبت تولید با محتوای دیگری استفاده شده است.")
            return existing
    count = accepted + rejected
    if (type(accepted) is not int or type(rejected) is not int or accepted < 0 or rejected < 0
            or count <= 0 or count > work.remaining_quantity or work.status != WorkOrder.RELEASED):
        raise ValidationError("نوبت تولید باید برای سفارش آزادشده، مثبت و در حد باقیمانده باشد.")
    if not reason.strip():
        raise ValidationError("نتیجه و دلیل کنترل کیفیت را ثبت کنید.")
    processed = work.quantity - work.remaining_quantity
    requirements = []
    for material in work.materials.select_related("item").order_by("item_id"):
        consumed = material.required_quantity - material.remaining_quantity
        target = int((Decimal(material.required_quantity) * (processed + count) / work.quantity)
                     .to_integral_value(rounding=ROUND_CEILING))
        requirements.append((material, max(target - consumed, 0)))
    item_ids = {material.item_id for material, _ in requirements} | {work.product.pk}
    items = {item.pk: item for item in Item.objects.select_for_update().filter(pk__in=item_ids).order_by("pk")}
    if any(items[material.item_id].stock < qty for material, qty in requirements):
        raise ValidationError("مواد برای همین نوبت تولید کافی نیست؛ هیچ مصرفی ثبت نشد.")
    batch = ProductionBatch.objects.create(work_order=work, accepted_quantity=accepted,
        rejected_quantity=rejected, quality_reason=reason.strip(),
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        **({"request_key": request_key} if request_key else {}))
    for material, qty in requirements:
        if not qty:
            continue
        item = items[material.item_id]
        before = item.stock
        item.stock -= qty
        item.save(update_fields=["stock"])
        ProductionBatchMaterial.objects.create(batch=batch, work_order_material=material,
                                                 quantity=qty, unit_cost=material.unit_cost)
        StockMovement.objects.create(item=item, work_order=work, production_batch=batch,
            source=StockMovement.MANUFACTURE_ISSUE, change=-qty, balance_before=before,
            balance_after=item.stock, note=f"مصرف {batch.number}")
    if accepted:
        product = items[work.product.pk]
        before = product.stock
        product.stock += accepted
        product.save(update_fields=["stock"])
        StockMovement.objects.create(item=product, work_order=work, production_batch=batch,
            source=StockMovement.MANUFACTURE_RECEIPT, change=accepted, balance_before=before,
            balance_after=product.stock, note=f"رسید قابل قبول {batch.number}")
    if work.remaining_quantity == 0:
        work.status = WorkOrder.COMPLETED
        work.completed_at = timezone.now()
        work.completed_by = batch.created_by
        work.save(update_fields=["status", "completed_at", "completed_by"])
    post_production_batch(batch, actor)
    record_audit(actor, "production_batch_completed", work, work.number,
                 {"batch_id": batch.pk, "accepted": accepted, "rejected": rejected,
                  "quality_reason": reason.strip()})
    return batch
