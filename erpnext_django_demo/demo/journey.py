"""Trace only explicit customer demand links; pooled stock is never a reservation."""
from django.urls import reverse

from .access import ROLE_MANAGER, ROLE_PRODUCTION, ROLE_PURCHASE, has_role
from .models import Order, ProductionPlan, WorkOrder


def build_order_journey(order, user):
    if order.kind != Order.SALES:
        return None
    rows = []
    for demand in order.lines.select_related("item").all():
        plans = demand.production_plans.select_related("product").prefetch_related(
            "lines__item", "work_orders__bom__product", "work_orders__materials__item",
            "purchase_orders__lines__item")
        for plan in plans:
            rows.append({"title": plan.number, "owner": "تولید", "status": plan.get_status_display(),
                         "required": plan.demand_quantity, "done": None,
                         "url": reverse("demo:production_plan_detail", args=[plan.pk])
                         if has_role(user, ROLE_MANAGER, ROLE_PRODUCTION, ROLE_PURCHASE) else ""})
            for line in plan.lines.all():
                rows.append({"title": f"نیاز مواد: {line.item.name}", "owner": "تولید / خرید",
                             "status": "مبنای برنامه؛ موجودی و دریافت باز مشترک، بدون رزرو",
                             "required": line.gross_requirement,
                             "done": line.allocated_stock + line.scheduled_receipts,
                             "remaining": line.net_requirement})
            for purchase in plan.purchase_orders.all():
                done = hasattr(purchase, "fulfillment")
                rows.append({"title": purchase.number, "owner": "خرید" if purchase.status == Order.DRAFT else "انبار",
                             "status": purchase.workflow_label,
                             "required": sum(x.quantity for x in purchase.lines.all()),
                             "done": sum(x.quantity for x in purchase.lines.all()) if done else 0,
                             "url": reverse("demo:order_detail", args=[purchase.pk])
                             if has_role(user, ROLE_MANAGER, ROLE_PURCHASE) else ""})
            if not plan.purchase_orders.exists():
                rows.append({"title": "خرید مرتبط", "owner": "خرید", "status": "سند مرتبط ثبت نشده"})
            for work in plan.work_orders.all():
                shortage = sum(max(x.required_quantity - x.item.stock, 0)
                               for x in work.materials.all()) if work.status != WorkOrder.COMPLETED else 0
                rows.append({"title": f"{work.number} — {work.product.name}", "owner": "تولید",
                             "status": f"{work.get_status_display()} · کمبود مواد: {shortage}",
                             "required": work.quantity,
                             "done": work.quantity if work.status == WorkOrder.COMPLETED else 0,
                             "url": reverse("demo:work_order_detail", args=[work.pk])
                             if has_role(user, ROLE_MANAGER, ROLE_PRODUCTION) else ""})
            if not plan.work_orders.exists():
                rows.append({"title": "تولید مرتبط", "owner": "تولید", "status": "سند مرتبط ثبت نشده"})
        if not plans:
            rows.append({"title": demand.item.name, "owner": "تولید",
                         "status": "برنامهٔ مرتبط ثبت نشده؛ تامین اختصاصی مشخص نیست",
                         "required": demand.quantity, "done": None,
                         "url": reverse("demo:production_plan_new") + f"?order_line={demand.pk}"
                         if has_role(user, ROLE_MANAGER, ROLE_PRODUCTION)
                         and order.status == Order.CONFIRMED and not hasattr(order, "fulfillment")
                         and demand.item.boms.filter(status="active").exists() else ""})
    fulfilled = hasattr(order, "fulfillment")
    invoice = getattr(order, "invoice", None)
    if order.status == Order.CANCELLED:
        blocker, owner = "سفارش لغو شده است", "فروش"
    elif order.status == Order.DRAFT:
        blocker, owner = "تکمیل و تایید سفارش", "فروش"
    elif not fulfilled:
        shortages = [x.item.name for x in order.lines.select_related("item") if x.item.stock < x.quantity]
        blocker, owner = (("کمبود محصول: " + "، ".join(shortages), "تولید / خرید")
                          if shortages else ("ثبت تحویل؛ موجودی جاری مشترک است و رزرو نشده", "انبار"))
    elif not invoice:
        blocker, owner = "صدور صورتحساب", "حسابداری / فروش"
    elif invoice.balance:
        blocker, owner = "پیگیری وصول ماندهٔ صورتحساب", "حسابداری / فروش"
    else:
        blocker, owner = "تحویل و تسویه تکمیل شده است", "—"
    for row in rows:
        if "remaining" not in row and row.get("done") is not None:
            row["remaining"] = max(row["required"] - row["done"], 0)
    return {"rows": rows, "blocker": blocker, "owner": owner,
            "delivered": fulfilled, "invoiced": bool(invoice),
            "settled": bool(invoice and invoice.balance == 0)}
