from io import StringIO

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.urls import reverse

from .access import ROLE_FINANCE, ROLE_INVENTORY, ROLE_PRODUCTION, ROLE_PURCHASE, ROLE_SALES
from .models import AuditEvent, Order, StockMovement
from .tests import AuthenticatedTestCase


class GuidanceTests(AuthenticatedTestCase):
    def test_page_and_presentation_routes_respect_each_role_and_are_read_only(self):
        call_command("seed_demo", stdout=StringIO())
        before = (Order.objects.count(), StockMovement.objects.count(), AuditEvent.objects.count())
        for role in (ROLE_SALES, ROLE_PURCHASE, ROLE_PRODUCTION, ROLE_FINANCE, ROLE_INVENTORY):
            self.manager.groups.set([Group.objects.get_or_create(name=role)[0]])
            page = self.client.get(reverse("demo:workspace"))
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, 'id="demo-guide-data"')
            guide = page.context["demo_guide"]
            urls = set(entry["url"] for entry in guide["journey"])
            self.assertNotIn(reverse("demo:dashboard"), urls)
            self.assertNotIn(reverse("demo:audit_events"), urls)
            for url in urls:
                with self.subTest(role=role, url=url):
                    self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(before, (Order.objects.count(), StockMovement.objects.count(), AuditEvent.objects.count()))

    def test_empty_database_still_has_a_useful_guide_and_login_does_not(self):
        page = self.client.get(reverse("demo:workspace"))
        self.assertEqual(len(page.context["demo_guide"]["page"]), 3)
        for entry in page.context["demo_guide"]["journey"]:
            self.assertNotIn("orders/", entry["url"])
        self.client.logout()
        login = self.client.get(reverse("login"))
        self.assertNotContains(login, 'id="demo-guide-data"')
