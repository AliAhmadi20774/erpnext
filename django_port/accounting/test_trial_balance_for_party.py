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
from parties.models import Customer, Supplier

from .fiscal import create_fiscal_year
from .ledger import LedgerLine, post_gl_entries
from .models import Account
from .trial_balance_for_party_report import trial_balance_for_party_report


class PartyTrialBalanceTests(TestCase):
    def setUp(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=usd)
        self.year = create_fiscal_year(year="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))
        assets = Account.objects.create(name="Assets - EX", account_name="Assets", company=self.company,
                                        root_type="Asset", is_group=True)
        self.bank = Account.objects.create(name="Bank - EX", account_name="Bank", company=self.company,
                                           parent_account=assets)
        self.receivable = Account.objects.create(name="Receivable - EX", account_name="Receivable", company=self.company,
                                                 parent_account=assets, account_type="Receivable")
        liabilities = Account.objects.create(name="Liabilities - EX", account_name="Liabilities", company=self.company,
                                             root_type="Liability", is_group=True)
        self.payable = Account.objects.create(name="Payable - EX", account_name="Payable", company=self.company,
                                              parent_account=liabilities, account_type="Payable")
        self.first = Customer.objects.create(name="C-1", customer_name="Alpha")
        self.second = Customer.objects.create(name="C-2", customer_name="Beta")
        self.supplier = Supplier.objects.create(name="S-1", supplier_name="Vendor")

    def post(self, day, number, lines, *, opening=False):
        post_gl_entries(company=self.company, posting_date=day,
                        voucher_type="Journal Entry", voucher_no=number,
                        lines=lines, is_opening=opening)

    def test_customer_opening_period_zero_balances_and_supplier_isolation(self):
        self.post(date(2025, 1, 1), "OPEN-1", (
            LedgerLine(self.receivable, debit=100, customer=self.first),
            LedgerLine(self.bank, credit=100),
        ), opening=True)
        self.post(date(2025, 2, 1), "JE-1", (
            LedgerLine(self.receivable, debit=50, customer=self.first),
            LedgerLine(self.bank, credit=50),
        ))
        self.post(date(2025, 3, 10), "OPEN-2", (
            LedgerLine(self.receivable, debit=20, customer=self.first),
            LedgerLine(self.bank, credit=20),
        ), opening=True)
        self.post(date(2025, 3, 15), "PAY-1", (
            LedgerLine(self.bank, debit=80),
            LedgerLine(self.receivable, credit=80, customer=self.first),
        ))
        self.post(date(2025, 3, 16), "JE-2", (
            LedgerLine(self.receivable, debit=40, customer=self.second),
            LedgerLine(self.bank, credit=40),
        ))
        self.post(date(2025, 3, 17), "PAY-2", (
            LedgerLine(self.bank, debit=40),
            LedgerLine(self.receivable, credit=40, customer=self.second),
        ))
        self.post(date(2025, 3, 18), "BILL-1", (
            LedgerLine(self.bank, debit=30),
            LedgerLine(self.payable, credit=30, supplier=self.supplier),
        ))
        options = dict(company=self.company, fiscal_year=self.year,
                       from_date=date(2025, 3, 1), to_date=date(2025, 3, 31),
                       party_type="Customer")
        report = trial_balance_for_party_report(**options)
        self.assertEqual([row.party for row in report.rows], [self.first.pk])
        self.assertEqual((report.rows[0].opening_debit, report.rows[0].debit,
                          report.rows[0].credit, report.rows[0].closing_debit),
                         (Decimal("170"), Decimal("0"), Decimal("80"), Decimal("90")))
        with_zero = trial_balance_for_party_report(**options, exclude_zero_balance_parties=False)
        self.assertEqual([row.party for row in with_zero.rows], [self.first.pk, self.second.pk])
        self.assertEqual((with_zero.rows[1].debit, with_zero.rows[1].credit,
                          with_zero.rows[1].closing_debit), (Decimal("40"), Decimal("40"), Decimal("0")))
        supplier = trial_balance_for_party_report(**(options | {"party_type": "Supplier"}))
        self.assertEqual([(row.party, row.closing_credit) for row in supplier.rows],
                         [(self.supplier.pk, Decimal("30"))])
        self.assertEqual(trial_balance_for_party_report(**(options | {"account": self.bank})).rows, ())
        with self.assertRaises(ValidationError):
            trial_balance_for_party_report(**(options | {"party_type": "Employee"}))

        url = reverse("trial_balance_for_party_report")
        params = {"company": self.company.pk, "fiscal_year": self.year.pk,
                  "from_date": "2025-03-01", "to_date": "2025-03-31",
                  "party_type": "Customer", "exclude_zero_balance_parties": "on"}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        user = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        page = self.client.get(url, params)
        self.assertContains(page, "Alpha")
        self.assertEqual([row.party for row in page.context["report"].rows], [self.first.pk])
        response = self.client.get(url, params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual((len(rows), rows[1][0], rows[-1][0], Decimal(rows[-1][6])),
                         (3, self.first.pk, "Total", Decimal("90")))
