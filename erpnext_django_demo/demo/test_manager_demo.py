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
        self.assertContains(self.client.get(reverse("demo:exceptions"), {"area": "sales"}), order.number)
        response = self.client.post(reverse("demo:order_dates", args=[order.pk]), {
            "due_date": timezone.localdate().isoformat(), "payment_due_date": "2026-12-01"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(any(x["label"] == order.number for x in build_exception_alerts()))
