from io import StringIO

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from .tests import AuthenticatedTestCase
from .models import Order, StockMovement
from .exceptions import build_exception_alerts


class PresentationDataTests(AuthenticatedTestCase):
    def test_ready_examples_dates_links_and_idempotency(self):
        call_command("seed_demo", stdout=StringIO())
        healthy = Order.objects.get(notes="DEMO-HEALTHY")
        receivable = Order.objects.get(notes="DEMO-RECEIVABLE")
        shortage = Order.objects.get(notes="DEMO-CUSTOMER-JOURNEY")
        self.assertEqual(healthy.workflow_label, "تسویه شده")
        self.assertEqual(healthy.invoice.balance, 0)
        self.assertGreater(receivable.invoice.balance, 0)
        self.assertTrue(hasattr(receivable, "fulfillment"))
        self.assertLess(receivable.invoice.due_date, timezone.localdate())
        self.assertGreater(shortage.due_date, timezone.localdate())
        self.assertEqual(shortage.lines.get().quantity, 40)
        self.assertTrue(any(row["affected"] == shortage.number for row in build_exception_alerts()))
        self.assertTrue(any(row["label"] == receivable.invoice.number for row in build_exception_alerts()))
        page = self.client.get(reverse("demo:dashboard"))
        self.assertEqual(len(page.context["demo_orders"]), 4)
        for sample in (healthy, receivable, shortage):
            self.assertContains(page, reverse("demo:order_detail", args=[sample.pk]))
        self.assertFalse(StockMovement.objects.filter(created_at__gt=timezone.now()).exists())
        count = Order.objects.count()
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Order.objects.count(), count)
