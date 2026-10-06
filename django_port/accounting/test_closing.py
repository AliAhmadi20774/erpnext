import csv
import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from geo.models import Country, Currency, CurrencyExchange
from organizations.models import Company
from projects.models import Project

from .closing import cancel_period_closing_voucher, submit_period_closing_voucher
from .closing_balance_report import closing_balance_report
from .fiscal import create_fiscal_year
from .ledger import LedgerLine, account_balance, post_gl_entries
from .models import Account, AccountClosingBalance, CostCenter, FinanceBook, GLEntry, PeriodClosingVoucher
from .periods import create_accounting_period
from .trial_balance_report import trial_balance_report


class PeriodClosingVoucherTests(TestCase):
    def setUp(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=usd)
        self.year = create_fiscal_year(year="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))
        assets = Account.objects.create(name="Assets - EX", account_name="Assets", company=self.company, root_type="Asset", is_group=True)
        self.bank = Account.objects.create(name="Bank - EX", account_name="Bank", company=self.company, parent_account=assets)
        income = Account.objects.create(name="Income - EX", account_name="Income", company=self.company, root_type="Income", is_group=True)
        self.sales = Account.objects.create(name="Sales - EX", account_name="Sales", company=self.company, parent_account=income)
        expenses = Account.objects.create(name="Expenses - EX", account_name="Expenses", company=self.company, root_type="Expense", is_group=True)
        self.rent = Account.objects.create(name="Rent - EX", account_name="Rent", company=self.company, parent_account=expenses)
        liabilities = Account.objects.create(name="Liabilities - EX", account_name="Liabilities", company=self.company, root_type="Liability", is_group=True)
        self.retained = Account.objects.create(name="Retained - EX", account_name="Retained", company=self.company, parent_account=liabilities)
        root = CostCenter.objects.create(name="Example - EX", cost_center_name="Example", company=self.company, is_group=True)
        self.center = CostCenter.objects.create(name="Main - EX", cost_center_name="Main", company=self.company, parent_cost_center=root)

    def voucher(self, *, name="PCV-1", start=date(2025, 1, 1), end=date(2025, 3, 31), account=None):
        return PeriodClosingVoucher.objects.create(
            name=name, company=self.company, fiscal_year=self.year,
            closing_account_head=account or self.retained,
            period_start_date=start, period_end_date=end, remarks="Close quarter",
        )

    def post_activity(self, *, date_value=date(2025, 2, 1), suffix="1", finance_book=None):
        post_gl_entries(
            company=self.company, posting_date=date_value, voucher_type="Journal Entry", voucher_no=f"SALES-{suffix}",
            lines=(LedgerLine(self.bank, debit=100, finance_book=finance_book),
                   LedgerLine(self.sales, credit=100, cost_center=self.center, finance_book=finance_book)),
        )
        post_gl_entries(
            company=self.company, posting_date=date_value, voucher_type="Journal Entry", voucher_no=f"RENT-{suffix}",
            lines=(LedgerLine(self.rent, debit=30, cost_center=self.center, finance_book=finance_book),
                   LedgerLine(self.bank, credit=30, finance_book=finance_book)),
        )

    def test_closing_posts_balanced_reversals_and_continues_next_period(self):
        self.post_activity()
        first = submit_period_closing_voucher(self.voucher())
        self.assertEqual(first.status, PeriodClosingVoucher.Status.SUBMITTED)
        self.assertEqual(GLEntry.objects.filter(voucher_type="Period Closing Voucher", voucher_no=first.name).count(), 3)
        self.assertEqual(account_balance(self.sales), Decimal("0"))
        self.assertEqual(account_balance(self.rent), Decimal("0"))
        self.assertEqual(account_balance(self.retained), Decimal("-70"))
        ordinary_sales = AccountClosingBalance.objects.get(
            period_closing_voucher=first, account=self.sales,
            is_period_closing_voucher_entry=False,
        )
        closing_sales = AccountClosingBalance.objects.get(
            period_closing_voucher=first, account=self.sales,
            is_period_closing_voucher_entry=True,
        )
        self.assertEqual((ordinary_sales.debit, ordinary_sales.credit), (Decimal("0"), Decimal("100")))
        self.assertEqual((closing_sales.debit, closing_sales.credit), (Decimal("100"), Decimal("0")))
        self.assertEqual(ordinary_sales.cost_center, self.center)
        with self.assertRaises(ValidationError):
            ordinary_sales.save()
        with self.assertRaises(ValidationError):
            AccountClosingBalance.objects.filter(pk=ordinary_sales.pk).delete()
        with self.assertRaises(ValidationError):
            submit_period_closing_voucher(first)
        with self.assertRaises(ValidationError):
            first.delete()
        with self.assertRaises(ValidationError):
            PeriodClosingVoucher.objects.filter(pk=first.pk).delete()
        with self.assertRaises(ValidationError):
            PeriodClosingVoucher.objects.filter(pk=first.pk).update(status="Draft")
        with self.assertRaises(ValidationError):
            post_gl_entries(
                company=self.company, posting_date=date(2025, 2, 2),
                voucher_type="Journal Entry", voucher_no="LATE-1",
                lines=(LedgerLine(self.bank, debit=1), LedgerLine(self.sales, credit=1, cost_center=self.center)),
            )
        self.post_activity(date_value=date(2025, 5, 1), suffix="2")
        second = submit_period_closing_voucher(self.voucher(name="PCV-2", start=date(2025, 4, 1), end=date(2025, 6, 30)))
        self.assertEqual(second.status, PeriodClosingVoucher.Status.SUBMITTED)
        self.assertEqual(account_balance(self.retained), Decimal("-140"))
        self.assertEqual(account_balance(self.sales), Decimal("0"))
        carried_sales = AccountClosingBalance.objects.get(
            period_closing_voucher=second, account=self.sales,
            is_period_closing_voucher_entry=False,
        )
        self.assertEqual(carried_sales.credit, Decimal("200"))
        self.assertEqual(AccountClosingBalance.objects.get(
            period_closing_voucher=second, account=self.retained,
            is_period_closing_voucher_entry=True,
        ).credit, Decimal("140"))

    def test_project_dimensions_survive_closing_and_snapshot_carry_forward(self):
        first_project = Project.objects.create(name="PROJ-1", project_name="First", company=self.company)
        second_project = Project.objects.create(name="PROJ-2", project_name="Second", company=self.company)

        def post_sale(project, amount, number, posting_date):
            post_gl_entries(
                company=self.company, posting_date=posting_date,
                voucher_type="Journal Entry", voucher_no=number,
                lines=(
                    LedgerLine(self.bank, debit=amount, project=project),
                    LedgerLine(self.sales, credit=amount, cost_center=self.center, project=project),
                ),
            )

        post_sale(first_project, 40, "SALE-1", date(2025, 2, 1))
        post_sale(second_project, 60, "SALE-2", date(2025, 2, 2))
        first = submit_period_closing_voucher(self.voucher())
        closing_rows = GLEntry.objects.filter(voucher_type="Period Closing Voucher", voucher_no=first.name)
        self.assertEqual(set(closing_rows.filter(account=self.sales).values_list("project_id", "debit")), {
            (first_project.pk, Decimal("40")), (second_project.pk, Decimal("60")),
        })
        self.assertEqual(set(closing_rows.filter(account=self.retained).values_list("project_id", "credit")), {
            (first_project.pk, Decimal("40")), (second_project.pk, Decimal("60")),
        })

        def snapshot_amounts(voucher, account, field, *, is_closing=False):
            return dict(AccountClosingBalance.objects.filter(
                period_closing_voucher=voucher, account=account,
                is_period_closing_voucher_entry=is_closing,
            ).values_list("project_id", field))

        expected = {first_project.pk: Decimal("40"), second_project.pk: Decimal("60")}
        self.assertEqual(snapshot_amounts(first, self.bank, "debit"), expected)
        self.assertEqual(snapshot_amounts(first, self.sales, "credit"), expected)
        self.assertEqual(snapshot_amounts(first, self.retained, "credit", is_closing=True), expected)

        post_sale(first_project, 10, "SALE-3", date(2025, 5, 1))
        second = submit_period_closing_voucher(self.voucher(
            name="PCV-2", start=date(2025, 4, 1), end=date(2025, 6, 30),
        ))
        carried = {first_project.pk: Decimal("50"), second_project.pk: Decimal("60")}
        self.assertEqual(snapshot_amounts(second, self.bank, "debit"), carried)
        self.assertEqual(snapshot_amounts(second, self.sales, "credit"), carried)
        self.assertEqual(snapshot_amounts(second, self.retained, "credit", is_closing=True), carried)

    def test_closing_balance_report_filters_and_exports_submitted_snapshot(self):
        first = Project.objects.create(name="PROJ-1", project_name="First", company=self.company)
        second = Project.objects.create(name="PROJ-2", project_name="Second", company=self.company)
        for project, amount in ((first, 40), (second, 60)):
            post_gl_entries(
                company=self.company, posting_date=date(2025, 2, 1),
                voucher_type="Journal Entry", voucher_no=f"SALE-{project.pk}",
                lines=(LedgerLine(self.bank, debit=amount, project=project),
                       LedgerLine(self.sales, credit=amount, cost_center=self.center, project=project)),
            )
        voucher = submit_period_closing_voucher(self.voucher())
        result = closing_balance_report(company=self.company, voucher=voucher, project=first)
        self.assertEqual(len(result.rows), 4)
        self.assertEqual((result.ordinary_debit, result.ordinary_credit,
                          result.closing_debit, result.closing_credit),
                         (Decimal("40"), Decimal("40"), Decimal("40"), Decimal("40")))
        self.assertEqual({row.project_id for row in result.rows}, {first.pk})
        self.assertEqual(closing_balance_report(company=self.company, voucher=voucher,
                                                account=self.sales, project=first).rows[0].balance, Decimal("-40"))

        url = reverse("closing_balance_report")
        params = {"company": self.company.pk, "voucher": voucher.pk, "project": first.pk}
        guest = self.client.get(url, params)
        self.assertEqual(guest.status_code, 302)
        user = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        page = self.client.get(url, params)
        self.assertContains(page, "PROJ-1")
        self.assertEqual({row.project_id for row in page.context["report"].rows}, {first.pk})
        response = self.client.get(url, params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len(rows), 5)
        self.assertEqual({row[6] for row in rows[1:]}, {first.pk})
        self.assertEqual({row[7] for row in rows[1:]}, {"Yes", "No"})

        other = Company.objects.create(name="Other", abbr="OT", country=self.company.country,
                                       default_currency=self.company.default_currency)
        self.assertContains(self.client.get(url, params | {"company": other.pk}),
                            "Select a submitted period closing voucher for this company.")
        self.assertContains(self.client.get(url, params | {"project": "invalid"}),
                            "Select a valid choice")

    def test_trial_balance_uses_closing_snapshot_and_later_gl_activity(self):
        self.post_activity()
        first = submit_period_closing_voucher(self.voucher())
        self.post_activity(date_value=date(2025, 5, 1), suffix="2")
        options = dict(company=self.company, fiscal_year=self.year,
                       from_date=date(2025, 4, 1), to_date=date(2025, 5, 31))
        report = trial_balance_report(**options)
        self.assertEqual(report.snapshot_voucher, first)
        rows = {row.account.pk: row for row in report.rows}
        self.assertEqual((rows[self.bank.pk].opening_debit, rows[self.bank.pk].period_debit,
                          rows[self.bank.pk].period_credit, rows[self.bank.pk].closing_debit),
                         (Decimal("70"), Decimal("100"), Decimal("30"), Decimal("140")))
        self.assertEqual((rows[self.retained.pk].opening_credit, rows[self.retained.pk].closing_credit),
                         (Decimal("70"), Decimal("70")))
        self.assertEqual((rows[self.sales.pk].opening_debit, rows[self.sales.pk].opening_credit,
                          rows[self.sales.pk].period_credit), (Decimal("0"), Decimal("0"), Decimal("100")))
        self.assertEqual((report.total_opening_debit, report.total_opening_credit,
                          report.total_period_debit, report.total_period_credit,
                          report.total_closing_debit, report.total_closing_credit),
                         (Decimal("70"), Decimal("70"), Decimal("130"), Decimal("130"),
                          Decimal("170"), Decimal("170")))
        leaf = trial_balance_report(**options, show_group_accounts=False)
        self.assertEqual(leaf.total_closing_debit, report.total_closing_debit)
        self.assertFalse(any(row.account.is_group for row in leaf.rows))
        without_closing = trial_balance_report(**options, with_period_closing_entry_for_opening=False)
        self.assertEqual(without_closing.snapshot_voucher, first)
        self.assertEqual({row.account.pk: row for row in without_closing.rows}[self.sales.pk].opening_credit,
                         Decimal("100"))

        user = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(user)
        params = {"company": self.company.pk, "fiscal_year": self.year.pk,
                  "from_date": "2025-04-01", "to_date": "2025-05-31",
                  "include_default_book_entries": "on",
                  "with_period_closing_entry_for_opening": "on",
                  "with_period_closing_entry_for_current_period": "on",
                  "show_group_accounts": "on", "show_net_values": "on"}
        url = reverse("trial_balance_report")
        page = self.client.get(url, params)
        self.assertContains(page, "PCV-1")
        csv_response = self.client.get(url, params | {"format": "csv"})
        csv_rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8"))))
        self.assertEqual(csv_rows[-1][0], "Total")
        self.assertEqual(Decimal(csv_rows[-1][8]), Decimal("170"))
        self.assertContains(self.client.get(url, params | {"from_date": "2024-12-31"}),
                            "Select dates inside the fiscal year")

    def test_trial_balance_can_show_gross_opening_and_closing_columns(self):
        self.post_activity()
        submit_period_closing_voucher(self.voucher())
        self.post_activity(date_value=date(2025, 5, 1), suffix="2")
        options = dict(company=self.company, fiscal_year=self.year,
                       from_date=date(2025, 4, 1), to_date=date(2025, 5, 31),
                       show_group_accounts=False)
        net = trial_balance_report(**options)
        gross = trial_balance_report(**options, show_net_values=False)
        rows = {row.account.pk: row for row in gross.rows}
        self.assertEqual((rows[self.bank.pk].opening_debit, rows[self.bank.pk].opening_credit,
                          rows[self.bank.pk].closing_debit, rows[self.bank.pk].closing_credit),
                         (Decimal("100"), Decimal("30"), Decimal("200"), Decimal("60")))
        self.assertEqual((rows[self.sales.pk].opening_debit, rows[self.sales.pk].opening_credit,
                          rows[self.sales.pk].closing_debit, rows[self.sales.pk].closing_credit),
                         (Decimal("100"), Decimal("100"), Decimal("100"), Decimal("200")))
        self.assertEqual((gross.total_opening_debit, gross.total_opening_credit,
                          gross.total_closing_debit, gross.total_closing_credit),
                         (Decimal("230"), Decimal("230"), Decimal("360"), Decimal("360")))
        self.assertEqual((net.total_opening_debit, net.total_opening_credit),
                         (Decimal("70"), Decimal("70")))
        group_rows = {row.account.pk: row for row in trial_balance_report(
            **(options | {"show_group_accounts": True}), show_net_values=False,
        ).rows}
        self.assertEqual((group_rows[self.bank.parent_account_id].opening_debit,
                          group_rows[self.bank.parent_account_id].opening_credit),
                         (Decimal("100"), Decimal("30")))

        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        params = {"company": self.company.pk, "fiscal_year": self.year.pk,
                  "from_date": "2025-04-01", "to_date": "2025-05-31",
                  "with_period_closing_entry_for_opening": "on",
                  "with_period_closing_entry_for_current_period": "on"}
        url = reverse("trial_balance_report")
        page = self.client.get(url, params)
        self.assertEqual(page.context["report"].total_opening_debit, Decimal("230"))
        csv_rows = list(csv.reader(io.StringIO(self.client.get(
            url, params | {"format": "csv"},
        ).content.decode("utf-8"))))
        self.assertEqual((Decimal(csv_rows[-1][4]), Decimal(csv_rows[-1][8])),
                         (Decimal("230"), Decimal("360")))

    def test_trial_balance_preserves_project_filter_across_snapshot_and_gl(self):
        first_project = Project.objects.create(name="PROJ-1", project_name="First", company=self.company)
        second_project = Project.objects.create(name="PROJ-2", project_name="Second", company=self.company)
        for number, project, amount, posting_date in (
            ("SALE-1", first_project, 40, date(2025, 2, 1)),
            ("SALE-2", second_project, 60, date(2025, 2, 2)),
        ):
            post_gl_entries(company=self.company, posting_date=posting_date,
                            voucher_type="Journal Entry", voucher_no=number,
                            lines=(LedgerLine(self.bank, debit=amount, project=project),
                                   LedgerLine(self.sales, credit=amount, cost_center=self.center, project=project)))
        submit_period_closing_voucher(self.voucher())
        post_gl_entries(company=self.company, posting_date=date(2025, 5, 1),
                        voucher_type="Journal Entry", voucher_no="SALE-3",
                        lines=(LedgerLine(self.bank, debit=10, project=first_project),
                               LedgerLine(self.sales, credit=10, cost_center=self.center, project=first_project)))
        options = dict(company=self.company, fiscal_year=self.year,
                       from_date=date(2025, 4, 1), to_date=date(2025, 5, 31),
                       project=first_project, show_group_accounts=False)
        result = trial_balance_report(**options)
        self.assertEqual((result.total_opening_debit, result.total_opening_credit,
                          result.total_period_debit, result.total_period_credit,
                          result.total_closing_debit, result.total_closing_credit),
                         (Decimal("40"), Decimal("40"), Decimal("10"), Decimal("10"),
                          Decimal("50"), Decimal("50")))
        other = Company.objects.create(name="Other", abbr="OT", country=self.company.country,
                                       default_currency=self.company.default_currency)
        foreign_project = Project.objects.create(name="PROJ-OT", project_name="Foreign", company=other)
        with self.assertRaises(ValidationError):
            trial_balance_report(**(options | {"project": foreign_project}))

    def test_trial_balance_reads_prior_gl_when_no_closing_snapshot_exists(self):
        self.post_activity()
        report = trial_balance_report(
            company=self.company, fiscal_year=self.year,
            from_date=date(2025, 3, 1), to_date=date(2025, 3, 31),
            show_group_accounts=False,
        )
        self.assertIsNone(report.snapshot_voucher)
        self.assertEqual((report.total_opening_debit, report.total_opening_credit,
                          report.total_period_debit, report.total_period_credit),
                         (Decimal("100"), Decimal("100"), Decimal("0"), Decimal("0")))
        rows = {row.account.pk: row for row in report.rows}
        self.assertEqual(rows[self.bank.pk].opening_debit, Decimal("70"))
        self.assertEqual(rows[self.sales.pk].opening_credit, Decimal("100"))

    def test_trial_balance_excludes_prior_year_profit_and_loss_after_older_snapshot(self):
        previous_year = create_fiscal_year(
            year="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
        )
        self.post_activity(date_value=date(2024, 5, 1), suffix="OLD-1")
        old_voucher = PeriodClosingVoucher.objects.create(
            name="PCV-2024", company=self.company, fiscal_year=previous_year,
            closing_account_head=self.retained, period_start_date=date(2024, 1, 1),
            period_end_date=date(2024, 6, 30), remarks="Partial prior year close",
        )
        submit_period_closing_voucher(old_voucher)
        self.post_activity(date_value=date(2024, 8, 1), suffix="OLD-2")
        report = trial_balance_report(
            company=self.company, fiscal_year=self.year,
            from_date=date(2025, 1, 1), to_date=date(2025, 1, 31),
            show_group_accounts=False,
        )
        rows = {row.account.pk: row for row in report.rows}
        self.assertEqual(rows[self.bank.pk].opening_debit, Decimal("140"))
        self.assertEqual(rows[self.retained.pk].opening_credit, Decimal("70"))
        self.assertNotIn(self.sales.pk, rows)
        self.assertNotIn(self.rent.pk, rows)
        unclosed = trial_balance_report(
            company=self.company, fiscal_year=self.year,
            from_date=date(2025, 1, 1), to_date=date(2025, 1, 31),
            show_group_accounts=False, show_unclosed_fy_pl_balances=True,
        )
        unclosed_rows = {row.account.pk: row for row in unclosed.rows}
        self.assertEqual(unclosed_rows[self.sales.pk].opening_credit, Decimal("100"))
        self.assertEqual(unclosed_rows[self.rent.pk].opening_debit, Decimal("30"))

    def test_trial_balance_current_closing_entries_are_independent_from_opening_option(self):
        self.post_activity()
        submit_period_closing_voucher(self.voucher())
        options = dict(company=self.company, fiscal_year=self.year,
                       from_date=date(2025, 1, 1), to_date=date(2025, 3, 31),
                       show_group_accounts=False)
        included = trial_balance_report(**options)
        excluded = trial_balance_report(**options, with_period_closing_entry_for_current_period=False)
        included_rows = {row.account.pk: row for row in included.rows}
        excluded_rows = {row.account.pk: row for row in excluded.rows}
        self.assertEqual((included_rows[self.sales.pk].period_debit,
                          included_rows[self.sales.pk].period_credit), (Decimal("100"), Decimal("100")))
        self.assertEqual((excluded_rows[self.sales.pk].period_debit,
                          excluded_rows[self.sales.pk].period_credit), (Decimal("0"), Decimal("100")))
        self.assertNotIn(self.retained.pk, excluded_rows)
        self.assertEqual((included.total_opening_debit, excluded.total_opening_debit),
                         (Decimal("0"), Decimal("0")))

    def test_trial_balance_presentation_currency_uses_report_date_rate(self):
        self.post_activity()
        submit_period_closing_voucher(self.voucher())
        self.post_activity(date_value=date(2025, 5, 1), suffix="2")
        eur = Currency.objects.create(name="EUR", enabled=True)
        options = dict(company=self.company, fiscal_year=self.year,
                       from_date=date(2025, 4, 1), to_date=date(2025, 5, 31),
                       presentation_currency=eur, show_group_accounts=False)
        with self.assertRaises(ValidationError):
            trial_balance_report(**options)
        CurrencyExchange.objects.create(
            date=date(2025, 5, 30), from_currency=self.company.default_currency,
            to_currency=eur, exchange_rate=Decimal("2"),
        )
        report = trial_balance_report(**options)
        self.assertEqual(report.currency, "EUR")
        self.assertEqual(report.presentation_rate_date, date(2025, 5, 30))
        self.assertEqual((report.total_opening_debit, report.total_opening_credit,
                          report.total_period_debit, report.total_period_credit,
                          report.total_closing_debit, report.total_closing_credit),
                         (Decimal("140"), Decimal("140"), Decimal("260"), Decimal("260"),
                          Decimal("340"), Decimal("340")))
        self.assertEqual({row.account.pk: row for row in report.rows}[self.bank.pk].closing_debit,
                         Decimal("280"))

        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        params = {"company": self.company.pk, "fiscal_year": self.year.pk,
                  "from_date": "2025-04-01", "to_date": "2025-05-31",
                  "presentation_currency": eur.pk,
                  "with_period_closing_entry_for_opening": "on",
                  "with_period_closing_entry_for_current_period": "on",
                  "show_group_accounts": "on", "show_net_values": "on"}
        url = reverse("trial_balance_report")
        self.assertContains(self.client.get(url, params), "Amounts in EUR")
        rows = list(csv.reader(io.StringIO(self.client.get(
            url, params | {"format": "csv"},
        ).content.decode("utf-8"))))
        self.assertEqual((rows[-1][8], rows[-1][-1]), ("340.000000000", "EUR"))
        CurrencyExchange.objects.create(
            date=date(2025, 5, 31), from_currency=self.company.default_currency,
            to_currency=eur, exchange_rate=Decimal("2.1"),
        )
        latest = trial_balance_report(**options)
        self.assertEqual((latest.presentation_rate_date, latest.total_closing_debit),
                         (date(2025, 5, 31), Decimal("357")))
        CurrencyExchange.objects.create(
            date=date(2025, 5, 31), from_currency=self.company.default_currency,
            to_currency=eur, exchange_rate=Decimal("2.2"),
            for_buying=True, for_selling=False,
        )
        with self.assertRaises(ValidationError):
            trial_balance_report(**options)

    def test_rejects_invalid_start_account_and_future_sequence(self):
        with self.assertRaises(ValidationError):
            self.voucher(start=date(2025, 1, 2))
        with self.assertRaises(ValidationError):
            self.voucher(account=self.bank)
        first = self.voucher()
        with self.assertRaises(ValidationError):
            self.voucher(name="PCV-2", start=date(2025, 4, 1), end=date(2025, 6, 30)).full_clean()
        submit_period_closing_voucher(first)
        with self.assertRaises(ValidationError):
            self.voucher(name="PCV-3", start=date(2025, 1, 1), end=date(2025, 6, 30))

    def test_closed_accounting_period_blocks_submission_atomically(self):
        self.post_activity()
        voucher = self.voucher()
        create_accounting_period(
            period_name="Closed Q1", company=self.company,
            start_date=date(2025, 1, 1), end_date=date(2025, 3, 31),
            closed_document_types=("Period Closing Voucher",),
        )
        with self.assertRaises(ValidationError):
            submit_period_closing_voucher(voucher)
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, PeriodClosingVoucher.Status.DRAFT)
        self.assertFalse(GLEntry.objects.filter(voucher_type="Period Closing Voucher").exists())

    def test_empty_period_can_close_without_ledger_entries(self):
        self.company.reporting_currency = Currency.objects.create(name="EUR", enabled=True)
        self.company.save()
        stale_draft = self.voucher()
        voucher = submit_period_closing_voucher(stale_draft)
        self.assertEqual(voucher.status, PeriodClosingVoucher.Status.SUBMITTED)
        self.assertFalse(GLEntry.objects.filter(voucher_type="Period Closing Voucher").exists())
        self.assertFalse(AccountClosingBalance.objects.exists())
        with self.assertRaises(ValidationError):
            stale_draft.delete()

    def test_previous_year_with_ledger_must_close_first(self):
        previous_year = create_fiscal_year(
            year="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
        )
        cash = Account.objects.create(name="Cash - EX", account_name="Cash", company=self.company, parent_account=self.bank.parent_account)
        post_gl_entries(
            company=self.company, posting_date=date(2024, 5, 1),
            voucher_type="Journal Entry", voucher_no="OLD-1",
            lines=(LedgerLine(self.bank, debit=10), LedgerLine(cash, credit=10)),
        )
        current = self.voucher()
        with self.assertRaises(ValidationError):
            submit_period_closing_voucher(current)
        old = PeriodClosingVoucher.objects.create(
            name="PCV-2024", company=self.company, fiscal_year=previous_year,
            closing_account_head=self.retained, period_start_date=date(2024, 1, 1),
            period_end_date=date(2024, 12, 31), remarks="Close old year",
        )
        submit_period_closing_voucher(old)
        self.assertEqual(submit_period_closing_voucher(current).status, PeriodClosingVoucher.Status.SUBMITTED)

    def test_rejects_foreign_profit_and_loss_without_partial_posting(self):
        eur = Currency.objects.create(name="EUR", enabled=True)
        euro_sales = Account.objects.create(
            name="Euro Sales - EX", account_name="Euro Sales", company=self.company,
            parent_account=self.sales.parent_account, account_currency=eur,
        )
        post_gl_entries(
            company=self.company, posting_date=date(2025, 2, 1),
            voucher_type="Journal Entry", voucher_no="EUR-1",
            lines=(
                LedgerLine(self.bank, debit=120),
                LedgerLine(euro_sales, credit_in_account_currency=100, exchange_rate=Decimal("1.2"), cost_center=self.center),
            ),
        )
        voucher = self.voucher()
        with self.assertRaises(ValidationError):
            submit_period_closing_voucher(voucher)
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, PeriodClosingVoucher.Status.DRAFT)
        self.assertFalse(GLEntry.objects.filter(voucher_type="Period Closing Voucher").exists())

    def test_rejects_stock_activity_until_stock_closing_is_ported(self):
        stock = Account.objects.create(
            name="Stock - EX", account_name="Stock", company=self.company,
            parent_account=self.bank.parent_account, account_type="Stock",
        )
        post_gl_entries(
            company=self.company, posting_date=date(2025, 2, 1),
            voucher_type="Journal Entry", voucher_no="STOCK-1",
            lines=(LedgerLine(stock, debit=10), LedgerLine(self.bank, credit=10)),
        )
        voucher = self.voucher()
        with self.assertRaises(ValidationError):
            submit_period_closing_voucher(voucher)
        self.assertFalse(GLEntry.objects.filter(voucher_type="Period Closing Voucher").exists())

    def test_reporting_currency_rate_is_required_and_failure_rolls_back(self):
        eur = Currency.objects.create(name="EUR", enabled=True)
        self.company.reporting_currency = eur
        self.company.save()
        self.post_activity()
        voucher = self.voucher()
        with self.assertRaises(ValidationError):
            submit_period_closing_voucher(voucher)
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, PeriodClosingVoucher.Status.DRAFT)
        self.assertFalse(GLEntry.objects.filter(voucher_type="Period Closing Voucher").exists())
        self.assertFalse(AccountClosingBalance.objects.exists())
        CurrencyExchange.objects.create(
            date=date(2025, 3, 31), from_currency=self.company.default_currency,
            to_currency=eur, exchange_rate=Decimal("1.2"),
        )
        submit_period_closing_voucher(voucher)
        retained = AccountClosingBalance.objects.get(
            period_closing_voucher=voucher, account=self.retained,
            is_period_closing_voucher_entry=True,
        )
        self.assertEqual(retained.credit_in_reporting_currency, Decimal("84"))
        self.assertEqual(retained.reporting_currency_exchange_rate, Decimal("1.2"))

    def test_first_snapshot_includes_balance_sheet_opening_rows(self):
        cash = Account.objects.create(
            name="Cash - EX", account_name="Cash", company=self.company,
            parent_account=self.bank.parent_account,
        )
        post_gl_entries(
            company=self.company, posting_date=date(2025, 1, 1),
            voucher_type="Opening Entry", voucher_no="OPEN-1", is_opening=True,
            lines=(LedgerLine(self.bank, debit=50), LedgerLine(cash, credit=50)),
        )
        voucher = submit_period_closing_voucher(self.voucher())
        self.assertEqual(AccountClosingBalance.objects.get(
            period_closing_voucher=voucher, account=self.bank,
            is_period_closing_voucher_entry=False,
        ).debit, Decimal("50"))
        self.assertEqual(AccountClosingBalance.objects.filter(period_closing_voucher=voucher).count(), 2)

    def test_snapshot_keeps_cost_centers_separate(self):
        second_center = CostCenter.objects.create(
            name="Secondary - EX", cost_center_name="Secondary", company=self.company,
            parent_cost_center=self.center.parent_cost_center,
        )
        post_gl_entries(
            company=self.company, posting_date=date(2025, 2, 1),
            voucher_type="Journal Entry", voucher_no="SPLIT-1",
            lines=(
                LedgerLine(self.bank, debit=100),
                LedgerLine(self.sales, credit=40, cost_center=self.center),
                LedgerLine(self.sales, credit=60, cost_center=second_center),
            ),
        )
        voucher = submit_period_closing_voucher(self.voucher())
        normal_sales = AccountClosingBalance.objects.filter(
            period_closing_voucher=voucher, account=self.sales,
            is_period_closing_voucher_entry=False,
        )
        self.assertEqual(set(normal_sales.values_list("cost_center_id", "credit")), {
            (self.center.pk, Decimal("40")), (second_center.pk, Decimal("60")),
        })
        closing_rows = AccountClosingBalance.objects.filter(
            period_closing_voucher=voucher, account=self.retained,
            is_period_closing_voucher_entry=True,
        )
        self.assertEqual(set(closing_rows.values_list("cost_center_id", "credit")), {
            (self.center.pk, Decimal("40")), (second_center.pk, Decimal("60")),
        })

    def test_closing_and_carry_forward_keep_finance_books_separate(self):
        first_book = FinanceBook.objects.create(finance_book_name="Statutory")
        second_book = FinanceBook.objects.create(finance_book_name="Management")
        self.post_activity(suffix="A", finance_book=first_book)
        self.post_activity(suffix="B", finance_book=second_book)
        first = submit_period_closing_voucher(self.voucher())
        sales = AccountClosingBalance.objects.filter(
            period_closing_voucher=first, account=self.sales,
            is_period_closing_voucher_entry=False,
        )
        self.assertEqual(set(sales.values_list("finance_book_id", "credit")), {
            (first_book.pk, Decimal("100")), (second_book.pk, Decimal("100")),
        })
        retained = AccountClosingBalance.objects.filter(
            period_closing_voucher=first, account=self.retained,
            is_period_closing_voucher_entry=True,
        )
        self.assertEqual(set(retained.values_list("finance_book_id", "credit")), {
            (first_book.pk, Decimal("70")), (second_book.pk, Decimal("70")),
        })
        self.post_activity(date_value=date(2025, 5, 1), suffix="C", finance_book=first_book)
        second = submit_period_closing_voucher(self.voucher(
            name="PCV-2", start=date(2025, 4, 1), end=date(2025, 6, 30),
        ))
        self.assertEqual(set(AccountClosingBalance.objects.filter(
            period_closing_voucher=second, account=self.retained,
            is_period_closing_voucher_entry=True,
        ).values_list("finance_book_id", "credit")), {
            (first_book.pk, Decimal("140")), (second_book.pk, Decimal("70")),
        })

    def test_cancel_period_closing_voucher_reverses_ledger_and_clears_snapshots(self):
        self.post_activity()
        voucher = submit_period_closing_voucher(self.voucher())
        self.assertEqual(voucher.status, PeriodClosingVoucher.Status.SUBMITTED)
        self.assertEqual(account_balance(self.sales), Decimal("0"))
        self.assertEqual(account_balance(self.rent), Decimal("0"))
        self.assertEqual(account_balance(self.retained), Decimal("-70"))
        self.assertTrue(voucher.closing_balances.exists())

        original_entries = list(GLEntry.objects.filter(voucher_type="Period Closing Voucher", voucher_no=voucher.name))
        self.assertEqual(len(original_entries), 3)

        cancelled = cancel_period_closing_voucher(voucher)
        self.assertEqual(cancelled.status, PeriodClosingVoucher.Status.CANCELLED)

        # Original entries must be marked is_cancelled=True
        for entry in GLEntry.objects.filter(pk__in=[e.pk for e in original_entries]):
            self.assertTrue(entry.is_cancelled)

        # Reverse entries must exist with is_cancelled=True and remarks
        reversal_entries = GLEntry.objects.filter(
            voucher_type="Period Closing Voucher",
            voucher_no=voucher.name,
            remarks=f"On cancellation of {voucher.name}",
        )
        self.assertEqual(reversal_entries.count(), 3)
        for rev in reversal_entries:
            self.assertTrue(rev.is_cancelled)

        # Total debit equals total credit across all voucher entries
        all_entries = GLEntry.objects.filter(voucher_type="Period Closing Voucher", voucher_no=voucher.name)
        self.assertEqual(all_entries.count(), 6)
        self.assertEqual(
            sum(e.debit for e in all_entries),
            sum(e.credit for e in all_entries),
        )

        # Balances returned to pre-closing state
        self.assertEqual(account_balance(self.sales), Decimal("-100"))
        self.assertEqual(account_balance(self.rent), Decimal("30"))
        self.assertEqual(account_balance(self.retained), Decimal("0"))

        # Snapshot records are purged
        self.assertEqual(voucher.closing_balances.count(), 0)
        self.assertFalse(AccountClosingBalance.objects.filter(period_closing_voucher=voucher).exists())

        # Cannot edit or delete cancelled voucher
        with self.assertRaises(ValidationError):
            cancelled.remarks = "Edited"
            cancelled.save()
        with self.assertRaises(ValidationError):
            cancelled.delete()
        with self.assertRaises(ValidationError):
            PeriodClosingVoucher.objects.filter(pk=cancelled.pk).delete()

        # Re-closing is now possible with a new voucher for the same period
        new_voucher = self.voucher(name="PCV-1-NEW")
        resubmitted = submit_period_closing_voucher(new_voucher)
        self.assertEqual(resubmitted.status, PeriodClosingVoucher.Status.SUBMITTED)
        self.assertEqual(account_balance(self.sales), Decimal("0"))
        self.assertEqual(account_balance(self.retained), Decimal("-70"))

    def test_cancel_blocked_if_later_period_closing_voucher_exists(self):
        self.post_activity()
        first = submit_period_closing_voucher(self.voucher())
        self.post_activity(date_value=date(2025, 5, 1), suffix="2")
        second = submit_period_closing_voucher(self.voucher(name="PCV-2", start=date(2025, 4, 1), end=date(2025, 6, 30)))

        # Attempting to cancel first while second is active must fail
        with self.assertRaises(ValidationError) as ctx:
            cancel_period_closing_voucher(first)
        self.assertIn("another Period Closing Entry PCV-2 exists after", str(ctx.exception))

        # Cancelling second first succeeds
        second = cancel_period_closing_voucher(second)
        self.assertEqual(second.status, PeriodClosingVoucher.Status.CANCELLED)

        # Now first can be cancelled
        first = cancel_period_closing_voucher(first)
        self.assertEqual(first.status, PeriodClosingVoucher.Status.CANCELLED)

    def test_cancel_requires_submitted_voucher(self):
        draft_voucher = self.voucher()
        with self.assertRaises(ValidationError):
            cancel_period_closing_voucher(draft_voucher)

        submitted = submit_period_closing_voucher(draft_voucher)
        cancel_period_closing_voucher(submitted)
        with self.assertRaises(ValidationError):
            cancel_period_closing_voucher(submitted)

    def test_cancel_blocked_by_closed_accounting_period(self):
        self.post_activity()
        voucher = submit_period_closing_voucher(self.voucher())

        # Close accounting period for Period Closing Voucher
        create_accounting_period(
            company=self.company,
            period_name="Q1-Closed",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 3, 31),
            closed_document_types=["Period Closing Voucher"],
        )
        with self.assertRaises(ValidationError) as ctx:
            cancel_period_closing_voucher(voucher)
        self.assertIn("closed", str(ctx.exception).lower())

    def test_admin_cancel_selected_action(self):
        from .admin import PeriodClosingVoucherAdmin
        from django.contrib.admin.sites import AdminSite
        from unittest.mock import Mock

        self.post_activity()
        voucher = submit_period_closing_voucher(self.voucher())
        admin_instance = PeriodClosingVoucherAdmin(PeriodClosingVoucher, AdminSite())
        request = Mock()
        request.user = None
        messages_sent = []
        admin_instance.message_user = lambda req, msg, level=None: messages_sent.append(msg)

        admin_instance.cancel_selected(request, PeriodClosingVoucher.objects.filter(pk=voucher.pk))
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, PeriodClosingVoucher.Status.CANCELLED)
        self.assertTrue(any("Cancelled PCV-1." in m for m in messages_sent))
