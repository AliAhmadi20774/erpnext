from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .large_product_demo import ROOT_SKU, ensure_large_product_tree
from .models import AuditEvent, BillOfMaterials, BOMComponent, Item, JournalLine, StockMovement
from .mrp import create_production_plan
from .product_structure import build_product_tree, product_tree_metrics
from .tests import AuthenticatedTestCase


class LargeProductDataTests(TestCase):
    def test_five_levels_costs_batch_output_and_shared_mrp_parts(self):
        call_command("seed_large_product_tree", stdout=StringIO())
        root = Item.objects.get(sku=ROOT_SKU)
        tree = build_product_tree(root)
        self.assertEqual((Item.objects.count(), BillOfMaterials.objects.count(), BOMComponent.objects.count()),
                         (115, 35, 126))
        metrics = product_tree_metrics(tree)
        self.assertEqual((metrics["level_count"], metrics["component_count"], metrics["leaf_count"]),
                         (5, 174, 128))
        self.assertEqual(len(tree["children"]), 8)
        self.assertGreater(metrics["shortage_count"], 0)
        self.assertEqual(build_product_tree(root, 3)["total_cost"], tree["total_cost"] * 3)
        wiring = Item.objects.get(sku="LP-S07-M2")
        wire = build_product_tree(wiring)["children"][0]
        self.assertEqual(wire["required"], Decimal("2.550"))
        self.assertEqual(tree["children"][3]["children"][2]["children"][0]["required"], 4)
        plan = create_production_plan(product_id=root.pk, demand_quantity=2,
                                      due_date=timezone.localdate() + timedelta(days=90))
        self.assertEqual(plan.lines.filter(item__sku="LP-DRIVE").aggregate(
            amount=Sum("gross_requirement"))["amount"], 14)
        self.assertEqual(plan.lines.filter(item__sku="LP-DRIVE-01").aggregate(
            amount=Sum("gross_requirement"))["amount"], 14)
        totals = JournalLine.objects.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(totals["debit"], totals["credit"])
        for item in Item.objects.all():
            self.assertEqual(item.stock, item.movements.aggregate(total=Sum("change"))["total"] or 0)

    def test_repeated_seed_preserves_edited_components_stock_and_history(self):
        self.assertTrue(ensure_large_product_tree())
        component = BOMComponent.objects.get(bom__product__sku=ROOT_SKU, item__sku="LP-S01")
        component.quantity = 2
        component.save()
        material = Item.objects.get(sku="LP-DRIVE-02")
        material.name = "قطعهٔ ویرایش‌شدهٔ کاربر"
        material.save()
        before = (StockMovement.objects.count(), AuditEvent.objects.count(), JournalLine.objects.count())
        self.assertFalse(ensure_large_product_tree())
        component.refresh_from_db()
        material.refresh_from_db()
        self.assertEqual(component.quantity, 2)
        self.assertEqual(material.name, "قطعهٔ ویرایش‌شدهٔ کاربر")
        self.assertEqual(before, (StockMovement.objects.count(), AuditEvent.objects.count(), JournalLine.objects.count()))

    def test_case_insensitive_code_collision_leaves_database_unchanged(self):
        existing = Item.objects.create(sku="lp-drive-01", name="قطعهٔ موجود", category="تجهیزات",
                                       purchase_price=123, sale_price=150, stock=0)
        with self.assertRaises(ValidationError):
            ensure_large_product_tree()
        existing.refresh_from_db()
        self.assertEqual((Item.objects.count(), BillOfMaterials.objects.count(), StockMovement.objects.count()), (1, 0, 0))
        self.assertEqual(existing.purchase_price, 123)


class LargeProductPageTests(AuthenticatedTestCase):
    def test_default_tree_and_selector_keep_selected_product_and_quantity(self):
        ensure_large_product_tree()
        page = self.client.get(reverse("demo:product_tree"))
        self.assertEqual(page.context["product"].sku, ROOT_SKU)
        self.assertContains(page, 'id="tree-product"')
        self.assertContains(page, "باز کردن همه")
        wiring = Item.objects.get(sku="LP-S07-M2")
        selected = self.client.get(reverse("demo:product_tree"), {"product": wiring.pk, "quantity": "3"})
        self.assertEqual(selected.context["product"].pk, wiring.pk)
        self.assertEqual(selected.context["plan_quantity"], 3)
        self.assertEqual(selected.context["tree"]["children"][0]["required"], Decimal("7.650"))
