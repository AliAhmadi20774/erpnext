from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .accounting import post_manufacturing
from .models import (BillOfMaterials, Item, ProductionPlan, ProductionPlanLine, StockMovement,
                     WorkOrder, WorkOrderMaterial, ProductionBatch, ProductionBatchMaterial)
from .services import record_audit


def _actor(actor):
    return actor if getattr(actor, "is_authenticated", False) else None


@transaction.atomic
def create_work_order(*, bom_id, quantity, planned_start, due_date, notes="", actor=None,
                      source_plan_id=None, source_plan_line_id=None):
    bom = BillOfMaterials.objects.select_for_update().select_related("product").get(pk=bom_id)
    if bom.status != BillOfMaterials.ACTIVE:
        raise ValidationError("فقط از نسخهٔ فعال BOM می‌توان سفارش ساخت ایجاد کرد.")
    quantity = int(quantity)
    if quantity <= 0:
        raise ValidationError("تعداد تولید باید بیشتر از صفر باشد.")
    if due_date < planned_start:
        raise ValidationError("موعد تکمیل نمی‌تواند پیش از تاریخ شروع باشد.")
    components = list(bom.components.select_related("item").order_by("sequence", "pk"))
    if not components:
        raise ValidationError("BOM فعال بدون جزء قابل برنامه‌ریزی نیست.")
    source_plan = None
    source_plan_line = None
    if source_plan_id or source_plan_line_id:
        if not source_plan_line_id:
            raise ValidationError("ردیف پیشنهاد MRP برای سفارش ساخت مشخص نشده است.")
        try:
            source_plan_line_id = int(source_plan_line_id)
            requested_plan_id = int(source_plan_id) if source_plan_id else None
        except (TypeError, ValueError):
            raise ValidationError("ارجاع برنامه و ردیف MRP معتبر نیست.")
        source_plan_line = ProductionPlanLine.objects.select_for_update().select_related(
            "plan").filter(pk=source_plan_line_id).first()
        if not source_plan_line:
            raise ValidationError("ردیف پیشنهاد MRP پیدا نشد.")
        source_plan = source_plan_line.plan
        if requested_plan_id and requested_plan_id != source_plan.pk:
            raise ValidationError("ردیف پیشنهاد به برنامهٔ MRP انتخاب‌شده تعلق ندارد.")
        if source_plan.status != ProductionPlan.OPEN:
            raise ValidationError("فقط برنامهٔ MRP باز می‌تواند مبنای سفارش ساخت باشد.")
        if (source_plan_line.supply_type != ProductionPlanLine.MAKE
                or source_plan_line.supply_bom_id != bom.pk
                or source_plan_line.net_requirement <= 0):
            raise ValidationError("این BOM در برنامهٔ MRP انتخاب‌شده پیشنهاد ساخت ندارد.")
        converted = sum(work.quantity - work.rejected_quantity for work in
                        WorkOrder.objects.filter(source_plan_line=source_plan_line).exclude(
                            status=WorkOrder.CANCELLED))
        remaining = max(source_plan_line.net_requirement - converted, 0)
        if quantity > remaining:
            raise ValidationError(
                f"مقدار سفارش از باقیماندهٔ پیشنهاد MRP ({remaining}) بیشتر است.")
    work_order = WorkOrder.objects.create(
        bom=bom, quantity=quantity, planned_start=planned_start, due_date=due_date,
        notes=notes.strip(), created_by=_actor(actor), source_plan=source_plan,
        source_plan_line=source_plan_line,
    )
    scale = Decimal(quantity) / bom.output_quantity
    WorkOrderMaterial.objects.bulk_create([
        WorkOrderMaterial(
            work_order=work_order, item=row.item,
            required_quantity=int((row.quantity * (Decimal("1") + row.scrap_percent / 100)
                                   * scale).to_integral_value(rounding=ROUND_CEILING)),
            unit_cost=row.item.purchase_price, sequence=row.sequence,
        )
        for row in components
    ])
    record_audit(actor, "work_order_created", work_order, work_order.number, {
        "bom_id": bom.pk, "bom_code": bom.code, "quantity": quantity,
        "source_plan_id": source_plan.pk if source_plan else None,
        "source_plan_line_id": source_plan_line.pk if source_plan_line else None,
    })
    return work_order


@transaction.atomic
def release_work_order(work_order_id, actor=None):
    work_order = WorkOrder.objects.select_for_update().select_related("bom").get(pk=work_order_id)
    if work_order.status != WorkOrder.DRAFT:
        raise ValidationError("فقط سفارش ساخت پیش‌نویس قابل آزادسازی است.")
    if work_order.bom.status != BillOfMaterials.ACTIVE:
        raise ValidationError("نسخهٔ BOM این سفارش دیگر فعال نیست؛ سفارش جدید بسازید.")
    if not work_order.materials.exists():
        raise ValidationError("سفارش ساخت بدون مواد مورد نیاز قابل آزادسازی نیست.")
    work_order.status = WorkOrder.RELEASED
    work_order.released_at = timezone.now()
    work_order.released_by = _actor(actor)
    work_order.save(update_fields=["status", "released_at", "released_by"])
    record_audit(actor, "work_order_released", work_order, work_order.number)
    return work_order


@transaction.atomic
def complete_work_order(work_order_id, actor=None):
    work_order = WorkOrder.objects.select_for_update().select_related(
        "bom__product").get(pk=work_order_id)
    if work_order.status != WorkOrder.RELEASED:
        raise ValidationError("فقط سفارش ساخت آزادشده قابل تکمیل است.")
    if work_order.production_batches.exists():
        from .partial_operations import produce_partial
        produce_partial(work_order.pk, work_order.remaining_quantity,
                        reason="تکمیل باقیمانده با پذیرش کنترل کیفیت", actor=actor)
        work_order.refresh_from_db()
        return work_order
    batch = ProductionBatch.objects.create(work_order=work_order,
        accepted_quantity=work_order.quantity, legacy=True,
        quality_reason="تکمیل باقیمانده با پذیرش کنترل کیفیت", created_by=_actor(actor))
    materials = list(work_order.materials.select_related("item").order_by("item_id"))
    item_ids = {row.item_id for row in materials} | {work_order.product.pk}
    items = {item.pk: item for item in Item.objects.select_for_update().filter(
        pk__in=item_ids).order_by("pk")}
    shortages = [row for row in materials if items[row.item_id].stock < row.required_quantity]
    if shortages:
        names = "، ".join(row.item.name for row in shortages)
        raise ValidationError(f"موجودی مواد برای تکمیل کافی نیست: {names}")

    movements = []
    for row in materials:
        item = items[row.item_id]
        before = item.stock
        item.stock -= row.required_quantity
        movements.append(StockMovement(
            item=item, work_order=work_order, source=StockMovement.MANUFACTURE_ISSUE,
            change=-row.required_quantity, balance_before=before, balance_after=item.stock,
            note=f"مصرف مواد {work_order.number}",
        ))
    product = items[work_order.product.pk]
    before = product.stock
    product.stock += work_order.quantity
    movements.append(StockMovement(
        item=product, work_order=work_order, source=StockMovement.MANUFACTURE_RECEIPT,
        change=work_order.quantity, balance_before=before, balance_after=product.stock,
        note=f"رسید تولید {work_order.number}",
    ))
    for item in items.values():
        item.save(update_fields=["stock"])
    for movement in movements:
        movement.production_batch = batch
    StockMovement.objects.bulk_create(movements)
    ProductionBatchMaterial.objects.bulk_create([
        ProductionBatchMaterial(batch=batch, work_order_material=row,
                                quantity=row.required_quantity, unit_cost=row.unit_cost)
        for row in materials])
    work_order.status = WorkOrder.COMPLETED
    work_order.completed_at = timezone.now()
    work_order.completed_by = _actor(actor)
    work_order.save(update_fields=["status", "completed_at", "completed_by"])
    post_manufacturing(work_order, actor)
    record_audit(actor, "work_order_completed", work_order, work_order.number, {
        "product_id": product.pk, "quantity": work_order.quantity,
        "materials": [{"item_id": row.item_id, "quantity": row.required_quantity}
                      for row in materials],
    })
    return work_order


@transaction.atomic
def cancel_work_order(work_order_id, actor=None):
    work_order = WorkOrder.objects.select_for_update().get(pk=work_order_id)
    if work_order.status not in (WorkOrder.DRAFT, WorkOrder.RELEASED):
        raise ValidationError("این سفارش ساخت قابل لغو نیست.")
    if work_order.movements.exists():
        raise ValidationError("سفارش ساخت دارای گردش انبار قابل لغو نیست.")
    work_order.status = WorkOrder.CANCELLED
    work_order.cancelled_at = timezone.now()
    work_order.save(update_fields=["status", "cancelled_at"])
    record_audit(actor, "work_order_cancelled", work_order, work_order.number)
    return work_order
