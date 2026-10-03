import csv
from datetime import date
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from .access import ROLE_SALES
from .models import FitGapItem, ManagementDecision
from .tests import AuthenticatedTestCase


class DecisionReportingTests(AuthenticatedTestCase):
    def setUp(self):
        self.decision = ManagementDecision.objects.create(meeting_date=date(2026, 10, 3))

    def add_need(self, title, **values):
        return FitGapItem.objects.create(
            decision=self.decision, area=values.pop("area", "sales"), title=title,
            requirement="Evidence still to be collected", **values)

    def test_decision_history_survives_deleted_editor_and_blank_display_name(self):
        editor = get_user_model().objects.create_user("former-editor")
        self.decision.updated_by = editor
        self.decision.save()
        editor.delete()
        page = self.client.get(reverse("demo:management_decisions"))
        self.assertContains(page, "کاربر در دسترس نیست")
        self.assertTrue(ManagementDecision.objects.filter(pk=self.decision.pk).exists())
        self.decision.updated_by = self.manager
        self.decision.save()
        self.assertContains(self.client.get(reverse("demo:management_decisions")), self.manager.username)

    def test_csv_preserves_zero_unknown_large_costs_and_formula_protection(self):
        self.add_need("Zero", cost_low=0, cost_high=0)
        self.add_need("Unknown")
        self.add_need("=unsafe", cost_low=98765432101234, cost_high=98765432101234)
        response = self.client.get(reverse("demo:decision_fit_gap_csv", args=[self.decision.pk]))
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig"))))
        by_title = {row[1]: row for row in rows[1:]}
        self.assertEqual(by_title["Zero"][12:14], ["0", "0"])
        self.assertEqual(by_title["Unknown"][12:14], ["", ""])
        self.assertEqual(by_title["'=unsafe"][12:14], ["98765432101234"] * 2)

    def test_filter_keeps_global_critical_warning_and_distinguishes_unknown_estimates(self):
        self.add_need("Critical tax gap", area="tax", priority="critical")
        self.add_need("Zero cost", cost_low=0, cost_high=0, status=FitGapItem.VALIDATED)
        self.add_need("Unestimated", status=FitGapItem.VALIDATED)
        page = self.client.get(reverse("demo:decision_fit_gap", args=[self.decision.pk]),
                               {"area": "sales", "status": FitGapItem.VALIDATED})
        self.assertEqual(len(page.context["rows"]), 2)
        self.assertEqual(page.context["global_blockers"], 1)
        self.assertEqual(page.context["blockers"], 0)
        self.assertContains(page, "صرف‌نظر از فیلتر")
        self.assertContains(page, "برآورد ثبت نشده")
        self.assertContains(page, "۰–۰")
        self.assertNotContains(page, "Critical tax gap")
        only_unknown = self.client.get(reverse("demo:decision_fit_gap", args=[self.decision.pk]),
                                       {"area": "tax"})
        self.assertContains(only_unknown, "برآورد ثبت نشده")

    def test_sales_cannot_read_decisions_or_export_their_costs(self):
        sales = get_user_model().objects.create_user("decision-sales")
        sales.groups.add(Group.objects.create(name=ROLE_SALES))
        self.client.force_login(sales)
        for name, args in (("management_decisions", []), ("decision_fit_gap", [self.decision.pk]),
                           ("decision_fit_gap_csv", [self.decision.pk])):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse("demo:" + name, args=args)).status_code, 403)
