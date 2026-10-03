import csv
from datetime import date
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from .access import ROLE_SALES
from .forms import FitGapItemForm, ManagementDecisionForm
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
        self.add_need("=unsafe", cost_low=999999999999999999, cost_high=999999999999999999)
        response = self.client.get(reverse("demo:decision_fit_gap_csv", args=[self.decision.pk]))
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig"))))
        by_title = {row[1]: row for row in rows[1:]}
        self.assertEqual(by_title["Zero"][12:14], ["0", "0"])
        self.assertEqual(by_title["Unknown"][12:14], ["", ""])
        self.assertEqual(by_title["'=unsafe"][12:14], ["999999999999999999"] * 2)

    def test_large_budget_round_trips_and_aggregate_exceeds_database_integer_range(self):
        amount = 999999999999999999
        self.decision.budget_ceiling = amount
        self.decision.save()
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.budget_ceiling, amount)
        for number in range(12):
            self.add_need(str(number), cost_low=amount, cost_high=amount,
                          status=FitGapItem.VALIDATED)
        page = self.client.get(reverse("demo:decision_fit_gap", args=[self.decision.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["budget_low"], 12 * amount)
        self.assertEqual(page.context["budget_high"], 12 * amount)

    def test_amount_forms_reject_fractions_negative_and_out_of_range_values(self):
        payload = {
            "area": "sales", "title": "Boundary", "requirement": "QA only",
            "fit": "unknown", "priority": "medium", "effort": "unknown",
            "risk": "medium", "phase": "discovery", "status": "draft",
        }
        for value in ("1.5", "-1", "1000000000000000000"):
            with self.subTest(value=value):
                gap = FitGapItemForm({**payload, "cost_low": value, "cost_high": value})
                self.assertFalse(gap.is_valid())
                self.assertIn("cost_low", gap.errors)
                self.assertIn("cost_high", gap.errors)
                decision = ManagementDecisionForm({"outcome": "pending", "architecture": "undecided",
                                                   "budget_ceiling": value})
                self.assertFalse(decision.is_valid())
                self.assertIn("budget_ceiling", decision.errors)
        for value in ("", "0", "999999999999999999"):
            with self.subTest(valid=value):
                self.assertTrue(FitGapItemForm({**payload, "cost_low": value, "cost_high": value}).is_valid())
                self.assertTrue(ManagementDecisionForm({"outcome": "pending", "architecture": "undecided",
                                                       "budget_ceiling": value}).is_valid())

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
