from io import StringIO
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.urls import reverse

from .models import AuditEvent, BillOfMaterials, Item, JournalLine, Order, ProductionPlan, StockMovement
from .professional_demo import ROOT, TAG, ensure_professional_demo
from .product_structure import build_product_tree, product_tree_metrics
from .tests import AuthenticatedTestCase


class ProfessionalDemoTests(AuthenticatedTestCase):
    def test_connected_industrial_workflow_prices_stock_and_financial_balance(self):
        call_command("seed_professional_demo", stdout=StringIO())
        root = Item.objects.get(sku=ROOT)
        tree = build_product_tree(root)
        self.assertEqual(product_tree_metrics(tree)["level_count"], 5)
        self.assertEqual(Item.objects.get(sku="PX-S06-M1-P1").unit, "رول")
        self.assertEqual(Item.objects.get(sku="PX-S07-M1-P1").purchase_price, 42500000)
        self.assertEqual(Item.objects.get(sku="PX-S07-M2-P3").purchase_price, 2500)
        self.assertGreater(root.sale_price, tree["total_cost"])
        order = Order.objects.get(notes=TAG)
        self.assertEqual(order.lines.get().quantity, 2)
        plan = ProductionPlan.objects.get(source_order_line__order=order)
        plc = plan.lines.get(item__sku="PX-S07-M1-P1")
        self.assertEqual(plc.net_requirement, 2)
        self.assertEqual(plan.lines.filter(item__sku="PX-DRIVE-01").aggregate(
            amount=Sum("gross_requirement"))["amount"], 14)
        self.assertEqual(plan.purchase_orders.get().source_plan_line_id, plc.pk)
        self.assertEqual(plan.purchase_orders.get().approval_status, "pending")
        self.assertEqual(plan.work_orders.get().quantity, 2)
        self.assertEqual(plan.scenarios.count(), 2)
        estimate = order.cost_estimates.get().snapshot
        self.assertTrue(Decimal(estimate["gross_profit"]) > 0)
        healthy = Order.objects.get(notes="DEMO-INDUSTRIAL-HEALTHY")
        receivable = Order.objects.get(notes="DEMO-INDUSTRIAL-RECEIVABLE")
        self.assertEqual(healthy.invoice.balance, 0)
        self.assertEqual(receivable.invoice.balance, receivable.invoice.amount/2)
        totals = JournalLine.objects.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(totals["debit"], totals["credit"])
        for item in Item.objects.all():
            balance = 0
            for movement in item.movements.order_by("created_at", "pk"):
                self.assertEqual(movement.balance_before, balance)
                balance += movement.change
                self.assertEqual(movement.balance_after, balance)
            self.assertEqual(item.stock, balance)
        page = self.client.get(reverse("demo:workspace"))
        urls = {entry["url"] for entry in page.context["demo_guide"]["journey"]}
        self.assertIn(reverse("demo:order_detail", args=[order.pk]), urls)
        detail = self.client.get(reverse("demo:order_detail", args=[order.pk]))
        self.assertContains(detail, "پروژهٔ توسعهٔ ظرفیت")
        self.assertNotContains(detail, TAG)

    def test_repeat_preserves_edits_and_history(self):
        self.assertTrue(ensure_professional_demo())
        root = Item.objects.get(sku=ROOT)
        root.name = "مدل ویرایش‌شدهٔ کاربر"
        root.save()
        before = (Order.objects.count(), StockMovement.objects.count(), AuditEvent.objects.count())
        self.assertFalse(ensure_professional_demo())
        root.refresh_from_db()
        self.assertEqual(root.name, "مدل ویرایش‌شدهٔ کاربر")
        self.assertEqual(before, (Order.objects.count(), StockMovement.objects.count(), AuditEvent.objects.count()))

    def test_conflict_rolls_back_without_overwriting_existing_item(self):
        Item.objects.create(sku="px-drive-01", name="کالای موجود", category="قطعه", sale_price=200, purchase_price=100)
        with self.assertRaises(ValidationError):
            ensure_professional_demo()
        self.assertEqual((Item.objects.count(), BillOfMaterials.objects.count(), Order.objects.count()), (1,0,0))
