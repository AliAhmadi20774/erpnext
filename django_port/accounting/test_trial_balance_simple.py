import csv
import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from geo.models import Country, Currency
from organizations.models import Company

from .fiscal import create_fiscal_year
from .ledger import LedgerLine, post_gl_entries
from .models import Account, FinanceBook
from .trial_balance_simple_report import trial_balance_simple_report


class SimpleTrialBalanceTests(TestCase):
    def setUp(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=usd)
        self.other = Company.objects.create(name="Other", abbr="OT", country=country, default_currency=usd)
        create_fiscal_year(year="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))
        create_fiscal_year(year="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        root = Account.objects.create(name="Assets - EX", account_name="Assets", company=self.company,
                                      root_type="Asset", is_group=True)
        self.bank = Account.objects.create(name="Bank - EX", account_name="Bank", company=self.company,
                                           parent_account=root)
        self.cash = Account.objects.create(name="Cash - EX", account_name="Cash", company=self.company,
                                           parent_account=root)
        other_root = Account.objects.create(name="Assets - OT", account_name="Assets", company=self.other,
                                            root_type="Asset", is_group=True)
        self.other_bank = Account.objects.create(name="Bank - OT", account_name="Bank", company=self.other,
                                                 parent_account=other_root)
        self.other_cash = Account.objects.create(name="Cash - OT", account_name="Cash", company=self.other,
                                                 parent_account=other_root)

    def post(self, company, bank, cash, day, amount, number, book=None):
        post_gl_entries(company=company, posting_date=day, voucher_type="Journal Entry", voucher_no=number,
                        lines=(LedgerLine(bank, debit=amount, finance_book=book),
                               LedgerLine(cash, credit=amount, finance_book=book)))

    def test_groups_by_fiscal_year_date_and_account_with_company_isolation(self):
        first = FinanceBook.objects.create(finance_book_name="A Book")
        second = FinanceBook.objects.create(finance_book_name="B Book")
        self.post(self.company, self.bank, self.cash, date(2025, 1, 1), 10, "JE-1", first)
        self.post(self.company, self.bank, self.cash, date(2025, 1, 1), 20, "JE-2", second)
        self.post(self.company, self.bank, self.cash, date(2025, 1, 2), 5, "JE-3")
        self.post(self.company, self.bank, self.cash, date(2026, 1, 1), 7, "JE-4")
        self.post(self.other, self.other_bank, self.other_cash, date(2025, 1, 1), 99, "OTHER")

        report = trial_balance_simple_report(company=self.company)
        self.assertEqual((len(report.rows), report.total_debit, report.total_credit),
                         (6, Decimal("42"), Decimal("42")))
        bank_rows = [row for row in report.rows if row.account == self.bank.pk]
        self.assertEqual([(row.fiscal_year, row.posting_date, row.debit, row.finance_book)
                          for row in bank_rows], [
            ("2025", date(2025, 1, 1), Decimal("30"), second.pk),
            ("2025", date(2025, 1, 2), Decimal("5"), None),
            ("2026", date(2026, 1, 1), Decimal("7"), None),
        ])
        self.assertEqual(trial_balance_simple_report(company=self.other).total_debit, Decimal("99"))

        url = reverse("trial_balance_simple_report")
        self.assertEqual(self.client.get(url, {"company": self.company.pk}).status_code, 302)
        user = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url, {"company": self.company.pk}).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        page = self.client.get(url, {"company": self.company.pk})
        self.assertContains(page, "Trial Balance (Simple)")
        self.assertEqual(len(page.context["report"].rows), 6)
        response = self.client.get(url, {"company": self.company.pk, "format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual((len(rows), rows[-1][0], Decimal(rows[-1][4]), Decimal(rows[-1][5])),
                         (8, "Total", Decimal("42"), Decimal("42")))
