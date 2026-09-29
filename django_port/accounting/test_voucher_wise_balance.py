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

from .fiscal import create_fiscal_year
from .ledger import LedgerLine, post_gl_entries
from .models import Account, GLEntry
from .voucher_wise_balance_report import voucher_wise_balance_report


class VoucherWiseBalanceTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=self.usd)
        self.other = Company.objects.create(name="Other", abbr="OT", country=country, default_currency=self.usd)
        self.year = create_fiscal_year(year="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))
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

    def imported_row(self, *, company, account, day, voucher_type, debit=0, credit=0, cancelled=False):
        return GLEntry(
            company=company, account=account, posting_date=day, fiscal_year=self.year,
            voucher_type=voucher_type, voucher_no="SHARED", account_currency=self.usd,
            debit=Decimal(str(debit)), credit=Decimal(str(credit)),
            debit_in_account_currency=Decimal(str(debit)),
            credit_in_account_currency=Decimal(str(credit)), is_cancelled=cancelled,
        )

    def test_reports_only_unbalanced_voucher_types_and_respects_filters(self):
        post_gl_entries(company=self.company, posting_date=date(2025, 1, 2),
                        voucher_type="Journal Entry", voucher_no="SHARED",
                        lines=(LedgerLine(self.bank, debit=10), LedgerLine(self.cash, credit=10)))
        self.assertEqual(voucher_wise_balance_report(company=self.company).rows, ())

        # Historical imports may contain partial rows. bulk_create models that import path.
        GLEntry.objects.bulk_create((
            self.imported_row(company=self.company, account=self.bank, day=date(2025, 1, 2),
                              voucher_type="Imported Invoice", debit=25),
            self.imported_row(company=self.company, account=self.cash, day=date(2025, 1, 3),
                              voucher_type="Imported Payment", credit=25),
            self.imported_row(company=self.company, account=self.bank, day=date(2025, 1, 2),
                              voucher_type="Cancelled Import", debit=5, cancelled=True),
            self.imported_row(company=self.other, account=self.other_bank, day=date(2025, 1, 2),
                              voucher_type="Other Company", debit=99),
        ))
        report = voucher_wise_balance_report(company=self.company)
        self.assertEqual([(row.voucher_type, row.debit, row.credit, row.difference) for row in report.rows], [
            ("Imported Invoice", Decimal("25"), Decimal("0"), Decimal("25")),
            ("Imported Payment", Decimal("0"), Decimal("25"), Decimal("-25")),
        ])
        self.assertEqual([row.voucher_type for row in voucher_wise_balance_report(
            company=self.company, from_date=date(2025, 1, 3), to_date=date(2025, 1, 3),
        ).rows], ["Imported Payment"])
        self.assertEqual([row.voucher_type for row in voucher_wise_balance_report(
            company=self.company, voucher_type="Imported Invoice",
        ).rows], ["Imported Invoice"])
        with self.assertRaises(ValidationError):
            voucher_wise_balance_report(company=self.company, from_date=date(2025, 1, 3),
                                        to_date=date(2025, 1, 2))

        url = reverse("voucher_wise_balance_report")
        params = {"company": self.company.pk}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        user = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        page = self.client.get(url, params)
        self.assertContains(page, "Imported Invoice")
        self.assertEqual(len(page.context["report"].rows), 2)
        response = self.client.get(url, params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual((len(rows), rows[1][0], Decimal(rows[2][4])),
                         (3, "Imported Invoice", Decimal("-25")))
