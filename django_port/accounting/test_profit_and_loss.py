import csv
import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from geo.models import Country, Currency
from organizations.models import Company

from .closing import submit_period_closing_voucher
from .fiscal import create_fiscal_year
from .ledger import LedgerLine, post_gl_entries
from .models import Account, CostCenter, PeriodClosingVoucher
from .profit_and_loss_report import profit_and_loss_comparison_report, profit_and_loss_report, profit_and_loss_yearly_report


class ProfitAndLossReportTests(TestCase):
    def setUp(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=usd)
        self.year = create_fiscal_year(year="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))
        assets = Account.objects.create(name="Assets - EX", account_name="Assets", company=self.company,
                                        root_type="Asset", is_group=True)
        self.bank = Account.objects.create(name="Bank - EX", account_name="Bank", company=self.company,
                                           parent_account=assets)
        income = Account.objects.create(name="Income - EX", account_name="Income", company=self.company,
                                        root_type="Income", is_group=True)
        self.sales = Account.objects.create(name="Sales - EX", account_name="Sales", company=self.company,
                                            parent_account=income)
        expenses = Account.objects.create(name="Expenses - EX", account_name="Expenses", company=self.company,
                                          root_type="Expense", is_group=True)
        self.rent = Account.objects.create(name="Rent - EX", account_name="Rent", company=self.company,
                                           parent_account=expenses)
        equity = Account.objects.create(name="Equity - EX", account_name="Equity", company=self.company,
                                        root_type="Equity", is_group=True)
        self.retained = Account.objects.create(name="Retained - EX", account_name="Retained",
                                               company=self.company, parent_account=equity)
        center_root = CostCenter.objects.create(name="Example - EX", cost_center_name="Example",
                                                company=self.company, is_group=True)
        self.center = CostCenter.objects.create(name="Main - EX", cost_center_name="Main",
                                                company=self.company, parent_cost_center=center_root)

    def sale(self, day, amount, number):
        post_gl_entries(company=self.company, posting_date=day,
                        voucher_type="Journal Entry", voucher_no=number,
                        lines=(LedgerLine(self.bank, debit=amount),
                               LedgerLine(self.sales, credit=amount, cost_center=self.center)))

    def test_income_expense_range_and_period_closing_exclusion(self):
        old_year = create_fiscal_year(year="2024", start_date=date(2024, 1, 1),
                                      end_date=date(2024, 12, 31))
        self.sale(date(2024, 2, 1), 50, "OLD")
        old_closing = PeriodClosingVoucher.objects.create(
            name="PCV-2024", company=self.company, fiscal_year=old_year,
            closing_account_head=self.retained, period_start_date=date(2024, 1, 1),
            period_end_date=date(2024, 12, 31), remarks="Close 2024",
        )
        submit_period_closing_voucher(old_closing)
        self.sale(date(2025, 2, 1), 100, "SALE")
        post_gl_entries(company=self.company, posting_date=date(2025, 3, 1),
                        voucher_type="Journal Entry", voucher_no="RENT",
                        lines=(LedgerLine(self.rent, debit=30, cost_center=self.center),
                               LedgerLine(self.bank, credit=30)))
        options = dict(company=self.company, fiscal_year=self.year,
                       from_date=date(2025, 1, 1), to_date=date(2025, 3, 31))
        before = profit_and_loss_report(**options)
        self.assertEqual((before.total_income, before.total_expense, before.net_profit_loss),
                         (Decimal("100"), Decimal("30"), Decimal("70")))
        self.assertEqual([(row.account.pk, row.amount) for row in before.income if not row.account.is_group],
                         [(self.sales.pk, Decimal("100"))])
        march = profit_and_loss_report(**(options | {"from_date": date(2025, 3, 1)}))
        self.assertEqual((march.total_income, march.total_expense, march.net_profit_loss),
                         (Decimal("0"), Decimal("30"), Decimal("-30")))

        voucher = PeriodClosingVoucher.objects.create(
            name="PCV-Q1", company=self.company, fiscal_year=self.year,
            closing_account_head=self.retained, period_start_date=date(2025, 1, 1),
            period_end_date=date(2025, 3, 31), remarks="Close Q1",
        )
        submit_period_closing_voucher(voucher)
        after = profit_and_loss_report(**options)
        self.assertEqual((after.total_income, after.total_expense, after.net_profit_loss),
                         (Decimal("100"), Decimal("30"), Decimal("70")))
        with self.assertRaises(ValidationError):
            profit_and_loss_report(**(options | {"to_date": date(2026, 1, 1)}))

    def test_page_csv_and_permission(self):
        self.sale(date(2025, 2, 1), 25, "SALE")
        url = reverse("profit_and_loss_report")
        params = {"company": self.company.pk, "fiscal_year": self.year.pk,
                  "from_date": "2025-01-01", "to_date": "2025-03-31"}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        self.assertContains(self.client.get(url, params), "Net Profit/Loss")
        response = self.client.get(url, params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        summary = {row[0]: Decimal(row[3]) for row in rows if row[0].startswith(("Total", "Net"))}
        self.assertEqual((summary["Total Income"], summary["Total Expense"],
                          summary["Net Profit/Loss"]),
                         (Decimal("25"), Decimal("0"), Decimal("25")))

    def test_monthly_comparison_activity_accumulation_closing_and_csv(self):
        self.sale(date(2025, 1, 10), 10, "JAN")
        self.sale(date(2025, 2, 10), 20, "FEB")
        post_gl_entries(company=self.company, posting_date=date(2025, 2, 20),
                        voucher_type="Journal Entry", voucher_no="RENT",
                        lines=(LedgerLine(self.rent, debit=5, cost_center=self.center),
                               LedgerLine(self.bank, credit=5)))
        closing = PeriodClosingVoucher.objects.create(
            name="PCV-Q1", company=self.company, fiscal_year=self.year,
            closing_account_head=self.retained, period_start_date=date(2025, 1, 1),
            period_end_date=date(2025, 3, 31), remarks="Close Q1",
        )
        submit_period_closing_voucher(closing)
        self.sale(date(2025, 4, 10), 30, "APR")
        options = dict(company=self.company, fiscal_year=self.year,
                       from_date=date(2025, 1, 1), to_date=date(2025, 4, 15))
        report = profit_and_loss_comparison_report(**options)
        rows = {row.label: row for row in report.rows}
        self.assertEqual(report.labels, ("2025-01-01 to 2025-01-31", "2025-02-01 to 2025-02-28",
                                         "2025-03-01 to 2025-03-31", "2025-04-01 to 2025-04-15"))
        self.assertEqual(rows["Total Income"].amounts,
                         (Decimal("10"), Decimal("20"), Decimal("0"), Decimal("30")))
        self.assertEqual(rows["Total Expense"].amounts,
                         (Decimal("0"), Decimal("5"), Decimal("0"), Decimal("0")))
        self.assertEqual(rows["Net Profit/Loss"].amounts,
                         (Decimal("10"), Decimal("15"), Decimal("0"), Decimal("30")))
        self.assertEqual(rows["Net Profit/Loss"].total, Decimal("55"))
        self.assertEqual(report.chart.kind, "bar")
        self.assertEqual([series.name for series in report.chart.series],
                         ["Income", "Expense", "Net Profit/Loss"])
        self.assertEqual([series.values for series in report.chart.series],
                         [rows[label].amounts for label in
                          ("Total Income", "Total Expense", "Net Profit/Loss")])
        self.assertEqual(len(report.chart.series[2].bars), len(report.labels))
        cumulative = profit_and_loss_comparison_report(**options, accumulated_values=True)
        cumulative_rows = {row.label: row for row in cumulative.rows}
        self.assertEqual(cumulative_rows["Net Profit/Loss"].amounts,
                         (Decimal("10"), Decimal("25"), Decimal("25"), Decimal("55")))
        self.assertEqual(cumulative_rows["Net Profit/Loss"].total, Decimal("55"))
        self.assertEqual(cumulative.chart.kind, "line")
        self.assertEqual(cumulative.chart.series[2].values,
                         cumulative_rows["Net Profit/Loss"].amounts)
        growth = profit_and_loss_comparison_report(**options, selected_view="Growth")
        growth_rows = {row.label: row for row in growth.rows}
        self.assertEqual(growth_rows["Net Profit/Loss"].amounts,
                         (Decimal("10"), Decimal("50.00"), Decimal("-100.00"), Decimal("100")))
        self.assertEqual(growth_rows["Total Expense"].amounts,
                         (Decimal("0"), Decimal("100"), Decimal("-100.00"), Decimal("0")))
        self.assertEqual(growth_rows["Net Profit/Loss"].total, Decimal("55"))
        self.assertEqual(growth.chart.series[2].values, rows["Net Profit/Loss"].amounts)
        margin = profit_and_loss_comparison_report(**options, selected_view="Margin")
        margin_rows = {row.label: row for row in margin.rows}
        self.assertEqual(margin_rows["Total Income"].amounts,
                         (Decimal("100.00"), Decimal("100.00"), Decimal("0"), Decimal("100.00")))
        self.assertEqual(margin_rows["Total Expense"].amounts,
                         (Decimal("0"), Decimal("25.00"), Decimal("0"), Decimal("0")))
        self.assertEqual(margin_rows["Net Profit/Loss"].amounts,
                         (Decimal("100.00"), Decimal("75.00"), Decimal("0"), Decimal("100.00")))
        self.assertEqual(margin_rows["Net Profit/Loss"].total, Decimal("55"))
        self.assertEqual(margin.chart.series[2].values, rows["Net Profit/Loss"].amounts)
        quarterly = profit_and_loss_comparison_report(**options, periodicity="Quarterly")
        self.assertEqual(quarterly.labels, ("2025-01-01 to 2025-03-31", "2025-04-01 to 2025-04-15"))
        self.assertEqual(next(row.amounts for row in quarterly.rows if row.label == "Net Profit/Loss"),
                         (Decimal("25"), Decimal("30")))
        partial = profit_and_loss_comparison_report(**(options | {"from_date": date(2025, 2, 15)}))
        self.assertEqual(next(row.amounts for row in partial.rows if row.label == "Net Profit/Loss"),
                         (Decimal("-5"), Decimal("0"), Decimal("30")))
        with self.assertRaises(ValidationError):
            profit_and_loss_comparison_report(**(options | {"periodicity": "Weekly"}))
        with self.assertRaises(ValidationError):
            profit_and_loss_comparison_report(**options, selected_view="Unknown")

        url = reverse("profit_and_loss_comparison_report")
        params = {"company": self.company.pk, "fiscal_year": self.year.pk,
                  "from_date": "2025-01-01", "to_date": "2025-04-15", "periodicity": "Monthly"}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        page = self.client.get(url, params)
        self.assertContains(page, "2025-04-01 to 2025-04-15")
        self.assertContains(page, "<svg")
        self.assertContains(page, "<rect")
        self.assertContains(self.client.get(url, params | {"accumulated_values": "1"}),
                            "<polyline")
        response = self.client.get(url, params | {"format": "csv"})
        csv_rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(csv_rows[0][2:6], list(report.labels))
        profit = next(row for row in csv_rows if row[1] == "Net Profit/Loss")
        self.assertEqual(tuple(map(Decimal, profit[2:6])), rows["Net Profit/Loss"].amounts)
        self.assertEqual(Decimal(profit[6]), rows["Net Profit/Loss"].total)
        margin_response = self.client.get(url, params | {"selected_view": "Margin", "format": "csv"})
        margin_csv = list(csv.reader(io.StringIO(margin_response.content.decode("utf-8"))))
        self.assertTrue(margin_csv[0][2].endswith("Margin %"))
        margin_profit = next(row for row in margin_csv if row[1] == "Net Profit/Loss")
        self.assertEqual(tuple(map(Decimal, margin_profit[2:6])), margin_rows["Net Profit/Loss"].amounts)
        self.assertEqual(Decimal(margin_profit[6]), Decimal("55"))
        growth_response = self.client.get(url, params | {"selected_view": "Growth", "format": "csv"})
        growth_csv = list(csv.reader(io.StringIO(growth_response.content.decode("utf-8"))))
        self.assertTrue(growth_csv[0][3].endswith("Growth %"))
        growth_profit = next(row for row in growth_csv if row[1] == "Net Profit/Loss")
        self.assertEqual(tuple(map(Decimal, growth_profit[2:6])), growth_rows["Net Profit/Loss"].amounts)
        self.assertEqual(Decimal(growth_profit[6]), Decimal("55"))

    def test_margin_view_handles_zero_income_with_nonzero_expense(self):
        post_gl_entries(company=self.company, posting_date=date(2025, 1, 10),
                        voucher_type="Journal Entry", voucher_no="RENT",
                        lines=(LedgerLine(self.rent, debit=5, cost_center=self.center),
                               LedgerLine(self.bank, credit=5)))
        report = profit_and_loss_comparison_report(
            company=self.company, fiscal_year=self.year,
            from_date=date(2025, 1, 1), to_date=date(2025, 1, 31),
            selected_view="Margin",
        )
        rows = {row.label: row for row in report.rows}
        self.assertEqual(rows["Total Income"].amounts, (Decimal("0"),))
        self.assertEqual(rows["Total Expense"].amounts, (None,))
        self.assertEqual(rows["Net Profit/Loss"].amounts, (None,))
        self.assertEqual(rows["Net Profit/Loss"].total, Decimal("-5"))
        self.assertEqual(report.chart.series[2].values, (Decimal("-5"),))
        self.assertGreater(report.chart.series[2].points[0].y, report.chart.zero_y)
        self.assertGreater(report.chart.series[2].bars[0].height, 0)

        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        url = reverse("profit_and_loss_comparison_report")
        params = {"company": self.company.pk, "fiscal_year": self.year.pk,
                  "from_date": "2025-01-01", "to_date": "2025-01-31",
                  "periodicity": "Monthly", "selected_view": "Margin"}
        self.assertContains(self.client.get(url, params), "—")
        response = self.client.get(url, params | {"format": "csv"})
        csv_rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        expense = next(row for row in csv_rows if row[1] == "Total Expense")
        self.assertEqual((expense[2], Decimal(expense[3])), ("", Decimal("5")))

    def test_yearly_comparison_aligns_accounts_and_rejects_foreign_year(self):
        old_year = create_fiscal_year(year="2024", start_date=date(2024, 1, 1),
                                      end_date=date(2024, 12, 31))
        self.sale(date(2024, 2, 1), 50, "OLD")
        post_gl_entries(company=self.company, posting_date=date(2024, 3, 1),
                        voucher_type="Journal Entry", voucher_no="OLD-RENT",
                        lines=(LedgerLine(self.rent, debit=10, cost_center=self.center),
                               LedgerLine(self.bank, credit=10)))
        self.sale(date(2025, 2, 1), 100, "NEW")
        new_income = Account.objects.create(name="Services - EX", account_name="Services",
                                            company=self.company, parent_account=self.sales.parent_account)
        post_gl_entries(company=self.company, posting_date=date(2025, 2, 2),
                        voucher_type="Journal Entry", voucher_no="SERVICE",
                        lines=(LedgerLine(self.bank, debit=20),
                               LedgerLine(new_income, credit=20, cost_center=self.center)))
        post_gl_entries(company=self.company, posting_date=date(2025, 3, 1),
                        voucher_type="Journal Entry", voucher_no="NEW-RENT",
                        lines=(LedgerLine(self.rent, debit=30, cost_center=self.center),
                               LedgerLine(self.bank, credit=30)))
        options = dict(company=self.company, from_fiscal_year=old_year, to_fiscal_year=self.year)
        report = profit_and_loss_yearly_report(**options)
        rows = {row.label: row for row in report.rows}
        self.assertEqual(report.labels, ("2024 (2024-12-31)", "2025 (2025-12-31)"))
        self.assertEqual(rows[new_income.name].amounts, (Decimal("0"), Decimal("20")))
        self.assertEqual(rows["Total Income"].amounts, (Decimal("50"), Decimal("120")))
        self.assertEqual(rows["Total Expense"].amounts, (Decimal("10"), Decimal("30")))
        self.assertEqual(rows["Net Profit/Loss"].amounts, (Decimal("40"), Decimal("90")))
        self.assertEqual(rows["Net Profit/Loss"].total, Decimal("130"))
        self.assertEqual(report.chart.series[2].values, rows["Net Profit/Loss"].amounts)
        self.assertLess([row.label for row in report.rows].index(new_income.name),
                        [row.label for row in report.rows].index("Total Income"))
        growth = profit_and_loss_yearly_report(**options, selected_view="Growth")
        self.assertEqual(next(row.amounts for row in growth.rows if row.label == "Net Profit/Loss"),
                         (Decimal("40"), Decimal("125.00")))
        margin = profit_and_loss_yearly_report(**options, selected_view="Margin")
        margin_profit = next(row for row in margin.rows if row.label == "Net Profit/Loss")
        self.assertEqual((margin_profit.amounts, margin_profit.total),
                         ((Decimal("80.00"), Decimal("75.00")), Decimal("130")))
        quarterly = profit_and_loss_yearly_report(**options, periodicity="Quarterly")
        quarterly_profit = next(row for row in quarterly.rows if row.label == "Net Profit/Loss")
        self.assertEqual(len(quarterly.labels), 8)
        self.assertEqual((quarterly.labels[0], quarterly.labels[4]),
                         ("2024 | 2024-01-01 to 2024-03-31",
                          "2025 | 2025-01-01 to 2025-03-31"))
        self.assertEqual(quarterly_profit.amounts,
                         (Decimal("40"), Decimal("0"), Decimal("0"), Decimal("0"),
                          Decimal("90"), Decimal("0"), Decimal("0"), Decimal("0")))
        self.assertEqual(quarterly_profit.total, Decimal("130"))
        self.assertEqual(quarterly.chart.series[2].values, quarterly_profit.amounts)
        accumulated = profit_and_loss_yearly_report(
            **options, periodicity="Quarterly", accumulated_values=True,
        )
        accumulated_profit = next(row for row in accumulated.rows if row.label == "Net Profit/Loss")
        self.assertEqual(accumulated_profit.amounts,
                         (Decimal("40"),) * 4 + (Decimal("90"),) * 4)
        self.assertEqual(accumulated_profit.total, Decimal("90"))
        self.assertEqual(accumulated.chart.kind, "line")
        self.assertEqual(accumulated.chart.series[2].values, accumulated_profit.amounts)
        accumulated_growth = profit_and_loss_yearly_report(
            **options, periodicity="Quarterly", accumulated_values=True,
            selected_view="Growth",
        )
        growth_profit = next(row for row in accumulated_growth.rows if row.label == "Net Profit/Loss")
        self.assertEqual(growth_profit.amounts[4], Decimal("125.00"))
        monthly = profit_and_loss_yearly_report(**options, periodicity="Monthly")
        monthly_profit = next(row for row in monthly.rows if row.label == "Net Profit/Loss")
        self.assertEqual(len(monthly.labels), 24)
        self.assertEqual((monthly_profit.amounts[1], monthly_profit.amounts[2],
                          monthly_profit.amounts[13], monthly_profit.amounts[14]),
                         (Decimal("50"), Decimal("-10"), Decimal("120"), Decimal("-30")))
        self.assertEqual(monthly_profit.total, Decimal("130"))
        self.assertEqual(len(monthly.chart.series[2].points), 24)
        with self.assertRaises(ValidationError):
            profit_and_loss_yearly_report(**options, periodicity="Weekly")
        with self.assertRaises(ValidationError):
            profit_and_loss_yearly_report(**(options | {"from_fiscal_year": self.year,
                                                      "to_fiscal_year": old_year}))
        other_company = Company.objects.create(name="Other", abbr="OT", country=self.company.country,
                                               default_currency=self.company.default_currency)
        foreign_year = create_fiscal_year(year="2026-OT", start_date=date(2026, 1, 1),
                                          end_date=date(2026, 12, 31), companies=(other_company,))
        with self.assertRaises(ValidationError):
            profit_and_loss_yearly_report(**(options | {"to_fiscal_year": foreign_year}))

        url = reverse("profit_and_loss_yearly_report")
        params = {"company": self.company.pk, "from_fiscal_year": old_year.pk,
                  "to_fiscal_year": self.year.pk}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        self.assertContains(self.client.get(url, params), "2024 (2024-12-31)")
        response = self.client.get(url, params | {"format": "csv"})
        csv_rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(csv_rows[0][2:4], list(report.labels))
        net = next(row for row in csv_rows if row[1] == "Net Profit/Loss")
        self.assertEqual((tuple(map(Decimal, net[2:4])), Decimal(net[4])),
                         (rows["Net Profit/Loss"].amounts, Decimal("130")))
        quarterly_response = self.client.get(url, params | {"periodicity": "Quarterly", "format": "csv"})
        quarterly_csv = list(csv.reader(io.StringIO(quarterly_response.content.decode("utf-8"))))
        self.assertEqual(quarterly_csv[0][2:10], list(quarterly.labels))
        quarterly_net = next(row for row in quarterly_csv if row[1] == "Net Profit/Loss")
        self.assertEqual((tuple(map(Decimal, quarterly_net[2:10])), Decimal(quarterly_net[10])),
                         (quarterly_profit.amounts, Decimal("130")))
