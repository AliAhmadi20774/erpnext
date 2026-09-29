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
from projects.models import Project

from .balance_sheet_report import balance_sheet_comparison_report, balance_sheet_report, balance_sheet_yearly_report
from .closing import submit_period_closing_voucher
from .fiscal import create_fiscal_year
from .ledger import LedgerLine, post_gl_entries
from .models import Account, CostCenter, FinanceBook, PeriodClosingVoucher


class BalanceSheetReportTests(TestCase):
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
        self.retained = Account.objects.create(name="Retained - EX", account_name="Retained", company=self.company,
                                               parent_account=equity)
        center_root = CostCenter.objects.create(name="Example - EX", cost_center_name="Example",
                                                company=self.company, is_group=True)
        self.center = CostCenter.objects.create(name="Main - EX", cost_center_name="Main",
                                                company=self.company, parent_cost_center=center_root)

    def sale(self, day, amount, number, *, cost_center=None, project=None, finance_book=None):
        post_gl_entries(company=self.company, posting_date=day,
                        voucher_type="Journal Entry", voucher_no=number,
                        lines=(LedgerLine(self.bank, debit=amount, cost_center=cost_center,
                                          project=project, finance_book=finance_book),
                               LedgerLine(self.sales, credit=amount, cost_center=cost_center or self.center,
                                          project=project, finance_book=finance_book)))

    def test_provisional_profit_moves_to_equity_after_period_closing(self):
        empty = balance_sheet_report(company=self.company, fiscal_year=self.year,
                                     as_of_date=date(2025, 1, 31))
        self.assertEqual((empty.assets, empty.total_assets), ((), Decimal("0")))
        self.sale(date(2025, 2, 1), 100, "SALE-1")
        post_gl_entries(company=self.company, posting_date=date(2025, 2, 2),
                        voucher_type="Journal Entry", voucher_no="RENT-1",
                        lines=(LedgerLine(self.rent, debit=30, cost_center=self.center),
                               LedgerLine(self.bank, credit=30)))
        before = balance_sheet_report(company=self.company, fiscal_year=self.year,
                                      as_of_date=date(2025, 3, 15))
        self.assertEqual((before.total_assets, before.total_liabilities, before.total_equity,
                          before.unclosed_prior_profit_loss, before.provisional_profit_loss,
                          before.total_credit),
                         (Decimal("70"), Decimal("0"), Decimal("0"), Decimal("0"),
                          Decimal("70"), Decimal("70")))
        self.assertEqual([(row.account.pk, row.amount) for row in before.assets if not row.account.is_group],
                         [(self.bank.pk, Decimal("70"))])

        voucher = PeriodClosingVoucher.objects.create(
            name="PCV-1", company=self.company, fiscal_year=self.year,
            closing_account_head=self.retained, period_start_date=date(2025, 1, 1),
            period_end_date=date(2025, 3, 31), remarks="Close Q1",
        )
        submit_period_closing_voucher(voucher)
        after = balance_sheet_report(company=self.company, fiscal_year=self.year,
                                     as_of_date=date(2025, 3, 31))
        self.assertEqual((after.total_assets, after.total_equity, after.provisional_profit_loss,
                          after.total_credit),
                         (Decimal("70"), Decimal("70"), Decimal("0"), Decimal("70")))
        self.assertEqual([(row.account.pk, row.amount) for row in after.equity if not row.account.is_group],
                         [(self.retained.pk, Decimal("70"))])
        with self.assertRaises(ValidationError):
            balance_sheet_report(company=self.company, fiscal_year=self.year,
                                 as_of_date=date(2026, 1, 1))

    def test_unclosed_prior_year_is_separate_from_current_profit_and_web_csv(self):
        create_fiscal_year(year="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
        self.sale(date(2024, 2, 1), 50, "OLD-SALE")
        self.sale(date(2025, 2, 1), 20, "NEW-SALE")
        report = balance_sheet_report(company=self.company, fiscal_year=self.year,
                                      as_of_date=date(2025, 3, 31))
        self.assertEqual((report.total_assets, report.unclosed_prior_profit_loss,
                          report.provisional_profit_loss, report.total_credit),
                         (Decimal("70"), Decimal("50"), Decimal("20"), Decimal("70")))
        self.assertEqual([(row.opening_amount, row.amount) for row in report.assets if not row.account.is_group],
                         [(Decimal("50"), Decimal("70"))])
        movement = balance_sheet_comparison_report(
            company=self.company, fiscal_year=self.year, from_date=date(2025, 2, 1),
            to_date=date(2025, 2, 28), accumulated_values=False,
        )
        movement_rows = {row.label: row.amounts for row in movement.rows}
        self.assertEqual((movement_rows["Total Assets"], movement_rows["Unclosed Prior Profit/Loss"],
                          movement_rows["Provisional Profit/Loss"]),
                         ((Decimal("20"),), (Decimal("50"),), (Decimal("-30"),)))

        url = reverse("balance_sheet_report")
        params = {"company": self.company.pk, "fiscal_year": self.year.pk, "as_of_date": "2025-03-31"}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        user = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        page = self.client.get(url, params)
        self.assertContains(page, "Unclosed Prior Profit/Loss")
        self.assertContains(page, "Provisional Profit/Loss")
        response = self.client.get(url, params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        summary = {row[0]: Decimal(row[4]) for row in rows[1:] if row[0].startswith(("Total", "Unclosed", "Provisional"))}
        self.assertEqual((summary["Total Assets"], summary["Unclosed Prior Profit/Loss"],
                          summary["Provisional Profit/Loss"], summary["Total Liabilities and Equity"]),
                         (Decimal("70"), Decimal("50"), Decimal("20"), Decimal("70")))

    def test_project_and_cost_center_filters_cover_both_sides_of_report(self):
        first = Project.objects.create(name="PROJ-1", project_name="First", company=self.company)
        second = Project.objects.create(name="PROJ-2", project_name="Second", company=self.company)
        other_center = CostCenter.objects.create(name="Other - EX", cost_center_name="Other",
                                                  company=self.company, parent_cost_center=self.center.parent_cost_center)
        self.sale(date(2025, 2, 1), 40, "SALE-1", cost_center=self.center, project=first)
        self.sale(date(2025, 2, 2), 60, "SALE-2", cost_center=other_center, project=second)
        options = dict(company=self.company, fiscal_year=self.year, as_of_date=date(2025, 3, 31))
        selected = balance_sheet_report(**options, cost_center=self.center, project=first)
        self.assertEqual((selected.total_assets, selected.provisional_profit_loss, selected.total_credit),
                         (Decimal("40"), Decimal("40"), Decimal("40")))
        self.assertEqual(balance_sheet_report(**options, project=second).total_assets, Decimal("60"))
        self.assertEqual(balance_sheet_report(**options).total_assets, Decimal("100"))

        url = reverse("balance_sheet_report")
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        params = {"company": self.company.pk, "fiscal_year": self.year.pk, "as_of_date": "2025-03-31",
                  "cost_center": self.center.pk, "project": first.pk}
        self.assertContains(self.client.get(url, params), "40")
        csv_response = self.client.get(url, params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8"))))
        self.assertEqual(Decimal(next(row[4] for row in rows if row[0] == "Total Assets")), Decimal("40"))

        other_company = Company.objects.create(name="Other", abbr="OT", country=self.company.country,
                                               default_currency=self.company.default_currency)
        foreign_project = Project.objects.create(name="PROJ-OT", project_name="Foreign", company=other_company)
        with self.assertRaises(ValidationError):
            balance_sheet_report(**options, project=foreign_project)

    def test_finance_book_filter_and_default_book_option(self):
        default_book = FinanceBook.objects.create(finance_book_name="Statutory")
        other_book = FinanceBook.objects.create(finance_book_name="Management")
        self.company.default_finance_book = default_book
        self.company.save(update_fields=["default_finance_book"])
        self.sale(date(2025, 2, 1), 10, "COMMON")
        self.sale(date(2025, 2, 2), 20, "STATUTORY", finance_book=default_book)
        self.sale(date(2025, 2, 3), 30, "MANAGEMENT", finance_book=other_book)
        options = dict(company=self.company, fiscal_year=self.year, as_of_date=date(2025, 3, 31))
        self.assertEqual(balance_sheet_report(**options).total_assets, Decimal("30"))
        management = balance_sheet_report(**options, finance_book=other_book,
                                          include_default_book_entries=False)
        self.assertEqual((management.total_assets, management.provisional_profit_loss),
                         (Decimal("40"), Decimal("40")))
        with self.assertRaises(ValidationError):
            balance_sheet_report(**options, finance_book=other_book, include_default_book_entries=True)

    def test_monthly_comparison_across_period_closing_and_partial_last_period(self):
        self.sale(date(2025, 1, 10), 10, "JAN")
        self.sale(date(2025, 2, 10), 20, "FEB")
        voucher = PeriodClosingVoucher.objects.create(
            name="PCV-Q1", company=self.company, fiscal_year=self.year,
            closing_account_head=self.retained, period_start_date=date(2025, 1, 1),
            period_end_date=date(2025, 3, 31), remarks="Close Q1",
        )
        submit_period_closing_voucher(voucher)
        self.sale(date(2025, 4, 10), 30, "APR")
        options = dict(company=self.company, fiscal_year=self.year,
                       from_date=date(2025, 1, 1), to_date=date(2025, 4, 15))
        report = balance_sheet_comparison_report(**options)
        self.assertEqual(report.dates, (date(2025, 1, 31), date(2025, 2, 28),
                                        date(2025, 3, 31), date(2025, 4, 15)))
        rows = {row.label: row.amounts for row in report.rows}
        self.assertEqual(rows["Total Assets"], (Decimal("10"), Decimal("30"),
                                                  Decimal("30"), Decimal("60")))
        self.assertEqual(rows["Total Equity"], (Decimal("0"), Decimal("0"),
                                                  Decimal("30"), Decimal("30")))
        self.assertEqual(rows["Provisional Profit/Loss"], (Decimal("10"), Decimal("30"),
                                                            Decimal("0"), Decimal("30")))
        self.assertEqual(rows["Total Liabilities and Equity"], rows["Total Assets"])
        quarterly = balance_sheet_comparison_report(**options, periodicity="Quarterly")
        self.assertEqual(quarterly.dates, (date(2025, 3, 31), date(2025, 4, 15)))
        growth = balance_sheet_comparison_report(**options, selected_view="Growth")
        growth_rows = {row.label: row.amounts for row in growth.rows}
        self.assertEqual(growth_rows["Total Assets"], (Decimal("10"), Decimal("200"),
                                                         Decimal("0"), Decimal("100")))
        self.assertEqual(growth_rows["Total Equity"], (Decimal("0"), Decimal("0"),
                                                         Decimal("100"), Decimal("0")))
        movement = balance_sheet_comparison_report(**options, accumulated_values=False)
        movement_rows = {row.label: row.amounts for row in movement.rows}
        self.assertEqual(movement_rows["Total Assets"], (Decimal("10"), Decimal("20"),
                                                           Decimal("0"), Decimal("30")))
        self.assertEqual(movement_rows["Total Equity"], (Decimal("0"), Decimal("0"),
                                                           Decimal("30"), Decimal("0")))
        self.assertEqual(movement_rows["Provisional Profit/Loss"],
                         (Decimal("10"), Decimal("20"), Decimal("-30"), Decimal("30")))
        self.assertEqual(movement_rows["Total Liabilities and Equity"], movement_rows["Total Assets"])
        self.assertEqual(movement.labels[-1], "2025-04-01 to 2025-04-15")
        midrange = balance_sheet_comparison_report(**(options | {"from_date": date(2025, 2, 15)}),
                                                   accumulated_values=False)
        self.assertEqual(next(row.amounts for row in midrange.rows if row.label == "Total Assets"),
                         (Decimal("0"), Decimal("0"), Decimal("30")))
        with self.assertRaises(ValidationError):
            balance_sheet_comparison_report(**(options | {"to_date": date(2026, 1, 1)}))
        with self.assertRaises(ValidationError):
            balance_sheet_comparison_report(**options, selected_view="Unknown")

        url = reverse("balance_sheet_comparison_report")
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
        self.assertContains(self.client.get(url, params), "2025-04-15")
        csv_response = self.client.get(url, params | {"format": "csv"})
        csv_rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8"))))
        self.assertEqual(csv_rows[0], ["Section", "Account", "2025-01-31", "2025-02-28",
                                       "2025-03-31", "2025-04-15", "Currency"])
        assets = next(row for row in csv_rows if row[1] == "Total Assets")
        self.assertEqual(tuple(map(Decimal, assets[2:6])), rows["Total Assets"])
        growth_response = self.client.get(url, params | {"selected_view": "Growth", "format": "csv"})
        growth_csv = list(csv.reader(io.StringIO(growth_response.content.decode("utf-8"))))
        self.assertEqual(growth_csv[0][2:6], ["2025-01-31", "2025-02-28 Growth %",
                                               "2025-03-31 Growth %", "2025-04-15 Growth %"])
        growth_assets = next(row for row in growth_csv if row[1] == "Total Assets")
        self.assertEqual(tuple(map(Decimal, growth_assets[2:6])), growth_rows["Total Assets"])
        movement_response = self.client.get(url, params | {"accumulated_values": "0", "format": "csv"})
        movement_csv = list(csv.reader(io.StringIO(movement_response.content.decode("utf-8"))))
        self.assertEqual(movement_csv[0][2:6], list(movement.labels))
        movement_assets = next(row for row in movement_csv if row[1] == "Total Assets")
        self.assertEqual(tuple(map(Decimal, movement_assets[2:6])), movement_rows["Total Assets"])

    def test_growth_view_handles_zero_and_negative_previous_balance(self):
        self.sale(date(2025, 2, 1), 10, "FEB")
        post_gl_entries(company=self.company, posting_date=date(2025, 3, 1),
                        voucher_type="Journal Entry", voucher_no="RENT",
                        lines=(LedgerLine(self.rent, debit=20, cost_center=self.center),
                               LedgerLine(self.bank, credit=20)))
        self.sale(date(2025, 4, 1), 15, "APR")
        report = balance_sheet_comparison_report(
            company=self.company, fiscal_year=self.year,
            from_date=date(2025, 1, 1), to_date=date(2025, 4, 30),
            selected_view="Growth",
        )
        amounts = next(row.amounts for row in report.rows if row.label == "Total Assets")
        self.assertEqual(amounts, (Decimal("0"), Decimal("100"), Decimal("-200"), Decimal("0")))

    def test_period_movement_keeps_account_that_reaches_zero_at_first_end(self):
        self.sale(date(2025, 1, 10), 10, "JAN")
        post_gl_entries(company=self.company, posting_date=date(2025, 2, 10),
                        voucher_type="Journal Entry", voucher_no="REVERSE",
                        lines=(LedgerLine(self.sales, debit=10, cost_center=self.center),
                               LedgerLine(self.bank, credit=10)))
        result = balance_sheet_comparison_report(
            company=self.company, fiscal_year=self.year,
            from_date=date(2025, 2, 1), to_date=date(2025, 2, 28),
            accumulated_values=False,
        )
        self.assertEqual(next(row.amounts for row in result.rows if row.label == self.bank.name),
                         (Decimal("-10"),))
        self.assertEqual(next(row.amounts for row in result.rows if row.label == "Total Assets"),
                         (Decimal("-10"),))

    def test_yearly_comparison_uses_each_year_end_and_aligns_new_accounts(self):
        old_year = create_fiscal_year(year="2024", start_date=date(2024, 1, 1),
                                      end_date=date(2024, 12, 31))
        self.sale(date(2024, 2, 1), 50, "OLD")
        self.sale(date(2025, 2, 1), 20, "NEW")
        other_bank = Account.objects.create(name="Other Bank - EX", account_name="Other Bank",
                                            company=self.company, parent_account=self.bank.parent_account)
        post_gl_entries(company=self.company, posting_date=date(2025, 3, 1),
                        voucher_type="Journal Entry", voucher_no="NEW-ACCOUNT",
                        lines=(LedgerLine(other_bank, debit=5),
                               LedgerLine(self.sales, credit=5, cost_center=self.center)))
        options = dict(company=self.company, from_fiscal_year=old_year, to_fiscal_year=self.year)
        report = balance_sheet_yearly_report(**options)
        self.assertEqual(report.dates, (date(2024, 12, 31), date(2025, 12, 31)))
        rows = {row.label: row.amounts for row in report.rows}
        self.assertEqual(rows[other_bank.name], (Decimal("0"), Decimal("5")))
        self.assertEqual(rows["Total Assets"], (Decimal("50"), Decimal("75")))
        self.assertEqual(rows["Unclosed Prior Profit/Loss"], (Decimal("0"), Decimal("50")))
        self.assertEqual(rows["Provisional Profit/Loss"], (Decimal("50"), Decimal("25")))
        self.assertEqual(rows["Total Liabilities and Equity"], rows["Total Assets"])
        self.assertLess([row.label for row in report.rows].index(other_bank.name),
                        [row.label for row in report.rows].index("Total Assets"))

        growth = balance_sheet_yearly_report(**options, selected_view="Growth")
        self.assertEqual(next(row.amounts for row in growth.rows if row.label == "Total Assets"),
                         (Decimal("50"), Decimal("50.00")))
        closing = PeriodClosingVoucher.objects.create(
            name="PCV-2024", company=self.company, fiscal_year=old_year,
            closing_account_head=self.retained, period_start_date=date(2024, 1, 1),
            period_end_date=date(2024, 12, 31), remarks="Close 2024",
        )
        submit_period_closing_voucher(closing)
        closed_rows = {row.label: row.amounts for row in balance_sheet_yearly_report(**options).rows}
        self.assertEqual(closed_rows["Total Assets"], (Decimal("50"), Decimal("75")))
        self.assertEqual(closed_rows["Total Equity"], (Decimal("50"), Decimal("50")))
        self.assertEqual(closed_rows["Unclosed Prior Profit/Loss"], (Decimal("0"), Decimal("0")))
        self.assertEqual(closed_rows["Provisional Profit/Loss"], (Decimal("0"), Decimal("25")))
        with self.assertRaises(ValidationError):
            balance_sheet_yearly_report(**(options | {"from_fiscal_year": self.year,
                                                    "to_fiscal_year": old_year}))
        other_company = Company.objects.create(name="Other", abbr="OT", country=self.company.country,
                                               default_currency=self.company.default_currency)
        foreign_year = create_fiscal_year(year="2026-OT", start_date=date(2026, 1, 1),
                                          end_date=date(2026, 12, 31), companies=(other_company,))
        with self.assertRaises(ValidationError):
            balance_sheet_yearly_report(**(options | {"to_fiscal_year": foreign_year}))

        url = reverse("balance_sheet_yearly_report")
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
        total_assets = next(row for row in csv_rows if row[1] == "Total Assets")
        self.assertEqual(tuple(map(Decimal, total_assets[2:4])), rows["Total Assets"])
