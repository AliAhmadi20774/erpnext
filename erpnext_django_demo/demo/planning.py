"""Read-only quantity and workday calculations shared by MRP and scenarios."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import BillOfMaterials, Item, Order, OrderLine, PlanningPolicy, WorkOrder


def workday(value, days, policy):
    direction = 1 if days >= 0 else -1
    remaining = abs(days)
    holidays = set(policy.holidays)
    policy.clean()
    while remaining:
        value += timedelta(days=direction)
        if value.weekday() in policy.working_weekdays and value.isoformat() not in holidays:
            remaining -= 1
    return value


def calculate_requirements(product_id, demand, due_date, lead_overrides=None, exclude_plan_id=None):
    demand = int(demand)
    if demand <= 0:
        raise ValidationError("تقاضا باید مثبت باشد.")
    today = timezone.localdate()
    policy = PlanningPolicy.objects.first() or PlanningPolicy()
    boms = {bom.product_id: bom for bom in BillOfMaterials.objects.filter(status="active").select_related(
        "product").prefetch_related("components__item")}
    if product_id not in boms:
        raise ValidationError("برای محصول BOM فعال وجود ندارد.")
    catalog = Item.objects.in_bulk()
    stock = {pk: item.stock for pk, item in catalog.items()}
    receipts = defaultdict(list)
    works = WorkOrder.objects.filter(status__in=[WorkOrder.DRAFT, WorkOrder.RELEASED])
    purchases = OrderLine.objects.filter(order__kind=Order.PURCHASE, order__status=Order.CONFIRMED,
                                         order__fulfillment__isnull=True).select_related("order")
    if exclude_plan_id:
        works = works.exclude(source_plan_id=exclude_plan_id)
        purchases = purchases.exclude(order__source_plan_id=exclude_plan_id)
    for work in works.select_related("bom"):
        receipts[work.bom.product_id].append([work.due_date, work.quantity])
    for line in purchases:
        receipts[line.item_id].append([line.order.due_date, line.quantity])
    rows = []
    lead_overrides = lead_overrides or {}

    def expand(pk, gross, need, parent_index, level, path):
        if pk in path:
            raise ValidationError("حلقه در BOM مانع برنامه‌ریزی شد.")
        item, bom = catalog[pk], boms.get(pk)
        allocated = min(stock[pk], gross)
        stock[pk] -= allocated
        remaining = gross - allocated
        scheduled, latest = 0, today
        late, unknown = 0, 0
        for receipt in sorted(receipts[pk], key=lambda entry: entry[0] or date.max):
            at, quantity = receipt
            if not at:
                unknown += quantity
            elif at > need:
                late += quantity
            else:
                used = min(quantity, remaining)
                receipt[1] -= used
                scheduled += used
                remaining -= used
                if used:
                    latest = max(latest, at)
        duration = bom.manufacturing_days if bom else int(lead_overrides.get(pk, item.lead_time_days))
        if not 0 <= duration <= 3650:
            raise ValidationError("مدت تامین باید بین صفر و ۳۶۵۰ روز باشد.")
        release = workday(need, -duration, policy)
        index = len(rows)
        row = {"item": item, "supply_bom": bom, "parent_index": parent_index, "level": level,
               "sequence": (index + 1) * 10, "gross_requirement": gross,
               "allocated_stock": allocated, "scheduled_receipts": scheduled,
               "net_requirement": remaining, "required_date": need, "release_date": release,
               "supply_type": "covered" if not remaining else "make" if bom else "buy",
               "duration": duration, "late_receipts": late, "unknown_receipts": unknown}
        rows.append(row)
        earliest = today
        if bom and remaining:
            children = []
            for component in bom.components.all():
                quantity = int((Decimal(remaining) * component.quantity / bom.output_quantity
                                * (1 + component.scrap_percent / 100)).to_integral_value(rounding=ROUND_CEILING))
                children.append(expand(component.item_id, quantity, release, index, level + 1, (*path, pk)))
            earliest = workday(max(children, default=today), duration, policy)
        elif remaining:
            earliest = workday(today, duration, policy)
        ready = max(earliest, latest)
        row["ready_date"] = ready
        return ready

    ready = expand(product_id, demand, due_date, None, 0, ())
    snapshot = {"calculated_on": today.isoformat(), "due_date": due_date.isoformat(),
                "estimated_delivery": ready.isoformat(), "feasible": ready <= due_date,
                "working_weekdays": policy.working_weekdays, "holidays": policy.holidays,
                "capacity_checked": False, "rows": [
                    {"sku": row["item"].sku, "name": row["item"].name,
                     "required_date": row["required_date"].isoformat(),
                     "release_date": row["release_date"].isoformat(),
                     "ready_date": row["ready_date"].isoformat(), "duration": row["duration"],
                     "late_receipts": row["late_receipts"], "unknown_receipts": row["unknown_receipts"]}
                    for row in rows]}
    return rows, snapshot
