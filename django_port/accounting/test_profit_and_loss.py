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
from .profit_and_loss_report import profit_and_loss_report


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
