from collections import defaultdict
from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.db.models import Prefetch
from django.utils import timezone

from .models import (BillOfMaterials, BOMComponent, Item, Order, OrderLine, ProductionPlan,
                     ProductionPlanLine, WorkOrder)
from .services import record_audit


def _ceil(value):
    return int(Decimal(value).to_integral_value(rounding=ROUND_CEILING))


@transaction.atomic
def create_production_plan(*, product_id, demand_quantity, due_date, notes="", actor=None,
                           source_order_line_id=None):
    product = Item.objects.select_for_update().get(pk=product_id)
    demand_quantity = int(demand_quantity)
    if demand_quantity <= 0:
        raise ValidationError("مقدار تقاضا باید بیشتر از صفر باشد.")
    source_line = None
    if source_order_line_id:
        source_line = OrderLine.objects.select_for_update().select_related("order").get(
            pk=source_order_line_id)
        if (source_line.order.kind != Order.SALES
                or source_line.order.status != Order.CONFIRMED
                or hasattr(source_line.order, "fulfillment")
                or source_line.item_id != product.pk
                or source_line.quantity != demand_quantity):
            raise ValidationError("برنامه باید با قلم فروش تاییدشدهٔ تحویل‌نشده و مقدار آن یکسان باشد.")
        if source_line.production_plans.filter(status=ProductionPlan.OPEN).exists():
            raise ValidationError("این قلم سفارش از قبل برنامهٔ MRP باز دارد.")
    active_boms = BillOfMaterials.objects.filter(status=BillOfMaterials.ACTIVE).select_related(
        "product").prefetch_related(Prefetch(
            "components",
            queryset=BOMComponent.objects.select_related("item").order_by("sequence", "pk"),
        ))
    by_product = {bom.product_id: bom for bom in active_boms}
    root_bom = by_product.get(product.pk)
    if not root_bom:
        raise ValidationError("برای محصول انتخاب‌شده BOM فعال وجود ندارد.")

    stock_pool = defaultdict(int, Item.objects.values_list("pk", "stock"))
    production_receipts = defaultdict(int)
    for row in WorkOrder.objects.filter(status__in=[WorkOrder.DRAFT, WorkOrder.RELEASED]).values(
            "bom__product_id").annotate(quantity=Sum("quantity")):
        production_receipts[row["bom__product_id"]] += row["quantity"]
    purchase_receipts = defaultdict(int)
    for row in OrderLine.objects.filter(
            order__kind=Order.PURCHASE, order__status=Order.CONFIRMED,
            order__fulfillment__isnull=True).values("item_id").annotate(quantity=Sum("quantity")):
        purchase_receipts[row["item_id"]] += row["quantity"]
    scheduled_pool = defaultdict(int)
    for item_id, quantity in production_receipts.items():
        scheduled_pool[item_id] += quantity
    for item_id, quantity in purchase_receipts.items():
        scheduled_pool[item_id] += quantity

    plan = ProductionPlan.objects.create(
        product=product, bom=root_bom, demand_quantity=demand_quantity, due_date=due_date,
        notes=notes.strip(),
        source_order_line=source_line,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    sequence = 0

    def expand(item, gross, parent, level, path):
        nonlocal sequence
        if item.pk in path:
            raise ValidationError("حلقه در ساختار BOM مانع اجرای MRP شد.")
        sequence += 10
        from_stock = min(stock_pool[item.pk], gross)
        stock_pool[item.pk] -= from_stock
        remaining = gross - from_stock
        from_scheduled = min(scheduled_pool[item.pk], remaining)
        scheduled_pool[item.pk] -= from_scheduled
        net = remaining - from_scheduled
        bom = by_product.get(item.pk)
        supply_type = (ProductionPlanLine.COVERED if net == 0 else
                       ProductionPlanLine.MAKE if bom else ProductionPlanLine.BUY)
        line = ProductionPlanLine.objects.create(
            plan=plan, parent=parent, item=item, supply_bom=bom, level=level,
            sequence=sequence, gross_requirement=gross, allocated_stock=from_stock,
            scheduled_receipts=from_scheduled, net_requirement=net,
            supply_type=supply_type,
        )
        if bom and net:
            for component in bom.components.all():
                required = _ceil(
                    Decimal(net) * component.quantity / bom.output_quantity
                    * (Decimal("1") + component.scrap_percent / Decimal("100")))
                expand(component.item, required, line, level + 1, (*path, item.pk))
        return line

    expand(product, demand_quantity, None, 0, ())
    record_audit(actor, "production_plan_created", plan, plan.number, {
        "product_id": product.pk, "demand_quantity": demand_quantity,
        "line_count": plan.lines.count(), "due_date": due_date.isoformat(),
        "source_order_line_id": source_order_line_id,
    })
    return plan


@transaction.atomic
def close_production_plan(plan_id, actor=None):
    plan = ProductionPlan.objects.select_for_update().get(pk=plan_id)
    if plan.status != ProductionPlan.OPEN:
        raise ValidationError("این برنامه قبلاً بسته شده است.")
    plan.status = ProductionPlan.CLOSED
    plan.closed_at = timezone.now()
    plan.closed_by = actor if getattr(actor, "is_authenticated", False) else None
    plan.save(update_fields=["status", "closed_at", "closed_by"])
    record_audit(actor, "production_plan_closed", plan, plan.number)
    return plan
