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
                           source_order_line_id=None, lead_overrides=None):
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
    from .planning import calculate_requirements
    rows, snapshot = calculate_requirements(product.pk, demand_quantity, due_date,
                                             lead_overrides=lead_overrides)
    plan = ProductionPlan.objects.create(
        product=product, bom=rows[0]["supply_bom"], demand_quantity=demand_quantity,
        due_date=due_date, notes=notes.strip(), source_order_line=source_line,
        schedule_snapshot=snapshot,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    created = []
    fields = ("item", "supply_bom", "level", "sequence", "gross_requirement",
              "allocated_stock", "scheduled_receipts", "net_requirement", "supply_type",
              "required_date", "release_date")
    for row in rows:
        parent = created[row["parent_index"]] if row["parent_index"] is not None else None
        created.append(ProductionPlanLine.objects.create(
            plan=plan, parent=parent, **{key: row[key] for key in fields}))
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
