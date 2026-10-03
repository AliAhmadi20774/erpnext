from datetime import timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Order, ProductionPlan
from .mrp import create_production_plan
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
