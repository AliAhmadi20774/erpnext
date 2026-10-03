from datetime import timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Order, ProductionPlan
from .mrp import create_production_plan
from .exceptions import build_exception_alerts
from .models import Invoice, Item, OrderLine, Customer
from .services import confirm_order, fulfill_order, issue_invoice, record_payment
from .tests import AuthenticatedTestCase
from .models import BillOfMaterials, BOMComponent, PlanningPolicy, Supplier
from .planning import calculate_requirements, workday


class CustomerJourneyTests(AuthenticatedTestCase):
    def setUp(self):
        call_command("seed_demo", stdout=StringIO())
        self.order = Order.objects.get(notes="DEMO-CUSTOMER-JOURNEY")
        self.line = self.order.lines.get()
        self.plan = self.line.production_plans.get()

    def test_explicit_demand_and_duplicate_plan_rejected(self):
        for quantity in (40, 39):
            with self.assertRaises(ValidationError):
                create_production_plan(product_id=self.line.item_id, demand_quantity=quantity,
                                       due_date=timezone.localdate() + timedelta(days=21),
                                       source_order_line_id=self.line.pk)
        self.assertEqual(self.line.production_plans.count(), 1)

    def test_journey_does_not_attribute_unlinked_supply(self):
        page = self.client.get(reverse("demo:order_detail", args=[self.order.pk]))
        self.assertContains(page, "مسیر یکپارچهٔ سفارش مشتری")
        self.assertContains(page, self.plan.number)
        self.assertContains(page, "بدون رزرو")
        self.assertNotContains(page, "WO-00001")
        self.assertEqual(page.context["journey"]["owner"], "تولید / خرید")

    def test_seed_idempotency_and_reset_with_linked_demand(self):
        count = ProductionPlan.objects.count()
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(ProductionPlan.objects.count(), count)
        call_command("reset_demo", "--yes", "--no-backup", stdout=StringIO())
        self.assertEqual(Order.objects.filter(notes="DEMO-CUSTOMER-JOURNEY").count(), 1)
        self.assertEqual(ProductionPlan.objects.filter(source_order_line__isnull=False).count(), 1)


class ExceptionTests(AuthenticatedTestCase):
    def test_deadline_boundary_unknown_and_resolution(self):
        item = Item.objects.create(name="قطعه", sku="EX-1", category="نمونه",
                                   stock=3, sale_price=100, purchase_price=50)
        customer = Customer.objects.create(name="مشتری", code="EX-C")
        order = Order.objects.create(kind=Order.SALES, customer=customer)
        OrderLine.objects.create(order=order, item=item, quantity=1, unit_price=100)
        confirm_order(order.pk)
        order.refresh_from_db()
        rows = build_exception_alerts()
        self.assertEqual(rows[0]["reason"], "موعد مشخص نشده")
        order.due_date = timezone.localdate()
        order.save()
        self.assertEqual(build_exception_alerts(), [])
        order.due_date -= timedelta(days=2)
        order.save()
        self.assertEqual(build_exception_alerts()[0]["days"], 2)
        fulfill_order(order.pk)
        self.assertEqual(build_exception_alerts(), [])
        invoice = issue_invoice(order.pk)
        invoice.due_date = timezone.localdate() - timedelta(days=1)
        invoice.save()
        self.assertEqual(build_exception_alerts()[0]["reason"], "مطالبهٔ سررسیدگذشته")
        record_payment(invoice.pk, 100)
        self.assertEqual(build_exception_alerts(), [])

    def test_filters_and_audited_date_update(self):
        call_command("seed_demo", stdout=StringIO())
        order = Order.objects.get(notes="DEMO-CUSTOMER-JOURNEY")
        Order.objects.filter(pk=order.pk).update(due_date=timezone.localdate() - timedelta(days=2))
        self.assertContains(self.client.get(reverse("demo:exceptions"), {"area": "sales"}), order.number)
        response = self.client.post(reverse("demo:order_dates", args=[order.pk]), {
            "due_date": timezone.localdate().isoformat(), "payment_due_date": "2026-12-01"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(any(x["label"] == order.number and x["key"].endswith(":delivery")
                             for x in build_exception_alerts()))


class PlanningTests(AuthenticatedTestCase):
    def setUp(self):
        self.material = Item.objects.create(name="ماده", sku="TIME-MAT", category="مواد",
                                            stock=0, purchase_price=100, sale_price=0, lead_time_days=5)
        self.product = Item.objects.create(name="محصول", sku="TIME-FG", category="محصول",
                                           stock=0, purchase_price=0, sale_price=1000)
        self.bom = BillOfMaterials.objects.create(product=self.product, code="TIME-BOM",
                                                   status="active", manufacturing_days=2)
        BOMComponent.objects.create(bom=self.bom, item=self.material, quantity=1)

    def test_calendar_skips_nonworking_days_and_holidays(self):
        from datetime import date
        policy = PlanningPolicy(working_weekdays=[5, 6, 0, 1, 2], holidays=["2026-10-03"])
        self.assertEqual(workday(date(2026, 10, 1), 1, policy), date(2026, 10, 4))
        self.assertEqual(workday(date(2026, 10, 4), -1, policy), date(2026, 9, 30))
        with self.assertRaises(ValidationError):
            workday(date(2026, 10, 1), 1, PlanningPolicy(working_weekdays=[]))

    def test_late_and_undated_receipts_are_not_on_time_supply(self):
        supplier = Supplier.objects.create(name="تامین", code="TIME-S")
        order = Order.objects.create(kind="purchase", supplier=supplier,
                                     due_date=timezone.localdate() + timedelta(days=30))
        OrderLine.objects.create(order=order, item=self.material, quantity=20, unit_price=100)
        confirm_order(order.pk)
        rows, snapshot = calculate_requirements(self.product.pk, 10,
                                                timezone.localdate() + timedelta(days=3))
        self.assertEqual(rows[1]["scheduled_receipts"], 0)
        self.assertEqual(rows[1]["late_receipts"], 20)
        self.assertFalse(snapshot["feasible"])
        changed_rows, changed = calculate_requirements(self.product.pk, 10,
                                                       timezone.localdate() + timedelta(days=3),
                                                       lead_overrides={self.material.pk: 10})
        self.assertGreater(changed["estimated_delivery"], snapshot["estimated_delivery"])

    def test_schedule_snapshot_survives_master_data_change(self):
        plan = create_production_plan(product_id=self.product.pk, demand_quantity=10,
                                       due_date=timezone.localdate() + timedelta(days=14))
        snapshot = plan.schedule_snapshot
        self.material.lead_time_days = 30
        self.material.save()
        plan.refresh_from_db()
        self.assertEqual(plan.schedule_snapshot, snapshot)
        page = self.client.get(reverse("demo:production_plan_detail", args=[plan.pk]))
        self.assertContains(page, "بدون کنترل ظرفیت")
