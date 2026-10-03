from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Item, PlanScenario, ProductionPlan
from .planning import calculate_requirements
from .product_structure import build_product_tree
from .services import record_audit


def material_estimate(product_id, quantity, price_overrides=None):
    tree = build_product_tree(Item.objects.get(pk=product_id), quantity)
    price_overrides = price_overrides or {}
    rows = []

    def walk(node):
        if node["children"]:
            for child in node["children"]:
                walk(child)
        else:
            item = node["item"]
            count = int(node["required"].to_integral_value(rounding=ROUND_CEILING))
            price = Decimal(price_overrides.get(item.pk, item.purchase_price))
            rows.append({"sku": item.sku, "name": item.name, "quantity": count,
                         "unit_price": str(price), "cost": str(count * price)})

    walk(tree)
    return {"rows": rows, "total": str(sum((Decimal(row["cost"]) for row in rows), Decimal(0)))}


def evaluate_scenario(plan, quantity, lead_overrides=None, price_overrides=None):
    rows, schedule = calculate_requirements(plan.product_id, quantity, plan.due_date,
                                             lead_overrides=lead_overrides, exclude_plan_id=plan.pk)
    return {"quantity": quantity, "schedule": schedule,
            "materials": material_estimate(plan.product_id, quantity, price_overrides),
            "buy_count": sum(row["supply_type"] == "buy" for row in rows),
            "make_count": sum(row["supply_type"] == "make" for row in rows),
            "lines": [{"sku": row["item"].sku, "name": row["item"].name,
                       "gross": row["gross_requirement"], "stock": row["allocated_stock"],
                       "receipts": row["scheduled_receipts"], "net": row["net_requirement"],
                       "type": row["supply_type"]} for row in rows]}


@transaction.atomic
def create_scenario(plan_id, *, label, quantity, item_id=None, lead_days=None, price=None, actor=None):
    plan = ProductionPlan.objects.get(pk=plan_id)
    quantity = int(quantity)
    if quantity <= 0:
        raise ValidationError("تقاضا باید مثبت باشد.")
    if item_id and not plan.lines.filter(item_id=item_id, supply_bom__isnull=True).exists():
        raise ValidationError("قطعه باید از اقلام خریدنی همین برنامه باشد.")
    if lead_days is not None and (not item_id or not 0 <= lead_days <= 3650):
        raise ValidationError("قطعه و مدت تامین معتبر لازم است.")
    if price is not None and (not item_id or not Decimal(price).is_finite() or Decimal(price) < 0):
        raise ValidationError("قیمت قطعه معتبر نیست.")
    params = {"quantity": quantity, "item_id": item_id, "lead_days": lead_days,
              "price": str(price) if price is not None else None}
    scenario = PlanScenario.objects.create(
        plan=plan, label=label.strip(), parameters=params,
        baseline=evaluate_scenario(plan, plan.demand_quantity),
        result=evaluate_scenario(plan, quantity,
                                 {item_id: lead_days} if lead_days is not None else None,
                                 {item_id: price} if price is not None else None),
        created_by=actor if getattr(actor, "is_authenticated", False) else None)
    record_audit(actor, "scenario_simulated", scenario, scenario.label, params)
    return scenario


@transaction.atomic
def apply_scenario(scenario_id, actor=None):
    from .mrp import create_production_plan
    scenario = PlanScenario.objects.select_for_update().select_related("plan").get(pk=scenario_id)
    if scenario.applied_plan_id:
        raise ValidationError("این سناریو قبلا به برنامهٔ اجرایی تبدیل شده است.")
    params = scenario.parameters
    plan = create_production_plan(product_id=scenario.plan.product_id,
                                   demand_quantity=params["quantity"], due_date=scenario.plan.due_date,
                                   notes=f"برنامهٔ مستقل از سناریو {scenario.pk}: {scenario.label}", actor=actor,
                                   lead_overrides={params["item_id"]: params["lead_days"]}
                                   if params["lead_days"] is not None else None)
    scenario.applied_plan = plan
    scenario.save(update_fields=["applied_plan"])
    record_audit(actor, "scenario_applied", scenario, scenario.label, {"applied_plan_id": plan.pk})
    return plan
