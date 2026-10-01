from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Prefetch

from .models import BillOfMaterials, BOMComponent


def build_product_tree(product, quantity=Decimal("1")):
    """Explode the active multi-level BOM and roll up material cost."""
    quantity = Decimal(str(quantity))
    active_boms = BillOfMaterials.objects.filter(status=BillOfMaterials.ACTIVE).select_related(
        "product"
    ).prefetch_related(Prefetch(
        "components",
        queryset=BOMComponent.objects.select_related("item").order_by("sequence", "pk"),
    ))
    by_product = {bom.product_id: bom for bom in active_boms}

    def expand(item, required, path):
        if item.pk in path:
            chain = " ← ".join(str(pk) for pk in (*path, item.pk))
            raise ValidationError(f"حلقه در ساختار محصول شناسایی شد: {chain}")
        bom = by_product.get(item.pk)
        children = []
        if bom:
            for component in bom.components.all():
                net_quantity = required * component.quantity / bom.output_quantity
                gross_quantity = net_quantity * (Decimal("1") + component.scrap_percent / Decimal("100"))
                child = expand(component.item, gross_quantity, (*path, item.pk))
                child["component"] = component
                child["net_quantity"] = net_quantity
                children.append(child)
            total_cost = sum((child["total_cost"] for child in children), Decimal("0"))
            unit_cost = total_cost / required if required else Decimal("0")
        else:
            unit_cost = item.purchase_price
            total_cost = required * unit_cost
        available = Decimal(item.stock)
        return {
            "item": item,
            "bom": bom,
            "required": required,
            "available": available,
            "shortage": max(required - available, Decimal("0")),
            "unit_cost": unit_cost,
            "total_cost": total_cost,
            "children": children,
        }

    return expand(product, quantity, ())


def product_tree_metrics(tree):
    nodes = []

    def walk(node, depth=0):
        nodes.append((node, depth))
        for child in node["children"]:
            walk(child, depth + 1)

    walk(tree)
    return {
        "component_count": max(len(nodes) - 1, 0),
        "level_count": max((depth for _, depth in nodes), default=0) + 1,
        "shortage_count": sum(node["shortage"] > 0 for node, _ in nodes[1:]),
        "leaf_count": sum(not node["children"] for node, _ in nodes),
    }


def validate_bom_activation(candidate):
    """Validate the graph as if candidate replaced the active BOM for its product."""
    active_boms = BillOfMaterials.objects.filter(status=BillOfMaterials.ACTIVE).exclude(
        product_id=candidate.product_id
    ).prefetch_related("components")
    by_product = {bom.product_id: bom for bom in active_boms}
    by_product[candidate.product_id] = candidate

    def visit(product_id, path):
        if product_id in path:
            raise ValidationError("فعال‌سازی این نسخه در ساختار محصول حلقه ایجاد می‌کند.")
        bom = by_product.get(product_id)
        if not bom:
            return
        for component in bom.components.all():
            visit(component.item_id, (*path, product_id))

    visit(candidate.product_id, ())
