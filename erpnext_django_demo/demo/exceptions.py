"""Current exceptions, with explicit deadlines and reversible live status."""
from django.urls import reverse
from django.utils import timezone

from .models import Invoice, Order, WorkOrder, ProductionPlan


def build_exception_alerts(today=None):
    today = today or timezone.localdate()
    alerts = []

    def add(obj, code, area, owner, reason, due, url, affected="—", critical=False):
        days = (today - due).days if due else 0
        alerts.append({"key": f"{obj._meta.model_name}:{obj.pk}:{code}",
                       "label": obj.number, "area": area, "owner": owner,
                       "reason": reason, "due": due, "affected": affected,
                       "url": url, "priority": 0 if critical else 1 if days > 0 else 2,
                       "severity": "بحرانی" if critical else "معوق" if days > 0 else "نیازمند بررسی",
                       "days": max(days, 0)})

    for order in Order.objects.filter(status=Order.CONFIRMED, fulfillment__isnull=True).select_related(
            "source_plan__source_order_line__order"):
        if not order.due_date or order.due_date < today:
            affected = (order.number if order.kind == Order.SALES else
                        order.source_plan.source_order_line.order.number
                        if order.source_plan and order.source_plan.source_order_line else "—")
            add(order, "delivery", order.kind, "انبار", "موعد مشخص نشده" if not order.due_date
                else "تحویل مشتری معوق" if order.kind == Order.SALES else "دریافت تامین معوق",
                order.due_date, reverse("demo:order_detail", args=[order.pk]), affected)
    for invoice in Invoice.objects.exclude(order__status=Order.CANCELLED).select_related("order").prefetch_related("payments"):
        if invoice.balance > 0 and (not invoice.due_date or invoice.due_date < today):
            add(invoice, "payment", "finance", "حسابداری", "سررسید مشخص نشده" if not invoice.due_date
                else "مطالبهٔ سررسیدگذشته" if invoice.order.kind == Order.SALES else "بدهی سررسیدگذشته",
                invoice.due_date, reverse("demo:order_detail", args=[invoice.order_id]),
                invoice.order.number if invoice.order.kind == Order.SALES else "—")
    for work in WorkOrder.objects.filter(status__in=[WorkOrder.DRAFT, WorkOrder.RELEASED]).select_related(
            "bom__product", "source_plan__source_order_line__order").prefetch_related("materials__item"):
        missing = [x.item.name for x in work.materials.all() if x.item.stock < x.required_quantity]
        if missing or work.due_date < today:
            affected = (work.source_plan.source_order_line.order.number
                        if work.source_plan and work.source_plan.source_order_line else "—")
            add(work, "production", "production", "تولید", "کمبود مواد: " + "، ".join(missing)
                if missing else "تکمیل تولید معوق", work.due_date,
                reverse("demo:work_order_detail", args=[work.pk]), affected, critical=bool(missing))
    from .planning import calculate_requirements
    for plan in ProductionPlan.objects.filter(status=ProductionPlan.OPEN).select_related("source_order_line__order"):
        demand = plan.source_order_line
        if not demand or demand.order.status != Order.CONFIRMED or hasattr(demand.order, "fulfillment"):
            continue
        due = demand.order.due_date or plan.due_date
        _, schedule = calculate_requirements(plan.product_id, plan.demand_quantity, due,
                                             exclude_plan_id=plan.pk)
        if not schedule["feasible"]:
            add(plan, "delivery_risk", "production", "تولید", "خطر تاخیر؛ آمادگی برآوردی: "
                + schedule["estimated_delivery"], due,
                reverse("demo:production_plan_detail", args=[plan.pk]), demand.order.number, critical=True)
    return sorted(alerts, key=lambda row: (row["priority"], -row["days"], row["key"]))
