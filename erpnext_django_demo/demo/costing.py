from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Item, Order, OrderCostEstimate
from .product_structure import build_product_tree
from .services import record_audit


def estimate_product(product_id, quantity, sale_price=None, price_overrides=None):
    from .scenarios import material_estimate
    product = Item.objects.get(pk=product_id)
    tree = build_product_tree(product, quantity)
    materials = material_estimate(product_id, quantity, price_overrides)
    labor, overhead = Decimal(0), Decimal(0)
    versions = []

    def walk(node):
        nonlocal labor, overhead
        if node["bom"]:
            bom = node["bom"]
            count = node["required"].to_integral_value(rounding=ROUND_CEILING)
            labor += count * bom.labor_cost_per_unit
            overhead += count * bom.overhead_cost_per_unit
            versions.append({"code": bom.code, "version": bom.version,
                             "quantity": str(count), "labor_rate": str(bom.labor_cost_per_unit),
                             "overhead_rate": str(bom.overhead_cost_per_unit)})
        for child in node["children"]:
            walk(child)

    walk(tree)
    total = Decimal(materials["total"]) + labor + overhead
    revenue = Decimal(sale_price if sale_price is not None else product.sale_price) * quantity
    profit = revenue - total
    margin = (profit / revenue * 100).quantize(Decimal("0.01")) if revenue else None
    return {"product": product.name, "sku": product.sku, "quantity": quantity,
            "materials": materials, "labor": str(labor), "overhead": str(overhead),
            "total": str(total), "revenue": str(revenue), "gross_profit": str(profit),
            "margin": str(margin) if margin is not None else None, "boms": versions}


@transaction.atomic
def create_order_estimate(order_id, actor=None):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.kind != Order.SALES or order.status == Order.CANCELLED:
        raise ValidationError("برآورد فقط برای سفارش فروش لغونشده قابل ثبت است.")
    lines = [estimate_product(row.item_id, row.quantity, row.unit_price)
             for row in order.lines.select_related("item")]
    if not lines:
        raise ValidationError("سفارش باید قلم داشته باشد.")
    totals = {key: sum((Decimal(line[key]) for line in lines), Decimal(0))
              for key in ("labor", "overhead", "total", "revenue", "gross_profit")}
    totals["materials"] = sum((Decimal(line["materials"]["total"]) for line in lines), Decimal(0))
    margin = (totals["gross_profit"] / totals["revenue"] * 100).quantize(Decimal("0.01")) if totals["revenue"] else None
    snapshot = {key: str(value) for key, value in totals.items()}
    snapshot.update(lines=lines, margin=str(margin) if margin is not None else None,
                    basis="BOM فعال، قیمت ثبت‌شدهٔ فروش، قیمت خرید فعلی، گردکردن قطعات خریدنی و نرخ هر واحد ساخت؛ برآورد بدون مالیات و هزینه‌های غیرتولیدی")
    estimate = OrderCostEstimate.objects.create(order=order, snapshot=snapshot,
        created_by=actor if getattr(actor, "is_authenticated", False) else None)
    record_audit(actor, "order_cost_estimated", estimate, order.number, snapshot)
    return estimate
