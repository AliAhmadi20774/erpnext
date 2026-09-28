from datetime import date
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from geo.models import Country, Currency
from organizations.models import Company

from .fiscal import create_fiscal_year, resolve_fiscal_year
from .ledger import LedgerLine, post_gl_entries
from .models import Account, FiscalYear, FiscalYearCompany, GLEntry


class FiscalYearTests(TestCase):
    def setUp(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.first = Company.objects.create(name="First", abbr="FI", country=country, default_currency=usd)
        self.second = Company.objects.create(name="Second", abbr="SE", country=country, default_currency=usd)

    def test_global_year_dates_overlap_and_boundary(self):
        year = create_fiscal_year(year="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        self.assertEqual(resolve_fiscal_year(date(2026, 1, 1), self.first), year)
        self.assertEqual(resolve_fiscal_year(date(2026, 12, 31), self.second), year)
        with self.assertRaises(ValidationError):
            create_fiscal_year(year="Bad length", start_date=date(2026, 4, 1), end_date=date(2026, 12, 31))
        with self.assertRaises(ValidationError):
            create_fiscal_year(year="Overlap", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30))
        with self.assertRaises(ValidationError):
            resolve_fiscal_year(date(2027, 1, 1), self.first)
        short = create_fiscal_year(year="2027 short", start_date=date(2027, 1, 1), end_date=date(2027, 3, 31), is_short_year=True)
        self.assertEqual(resolve_fiscal_year(date(2027, 3, 31), self.first), short)
        short.disabled = True
        short.save()
        with self.assertRaises(ValidationError):
            resolve_fiscal_year(date(2027, 3, 31), self.first)

    def test_company_scope_precedence_and_atomic_overlap(self):
        global_year = create_fiscal_year(year="Global 2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        scoped = create_fiscal_year(year="First 2026", start_date=date(2026, 4, 1), end_date=date(2027, 3, 31), companies=(self.first,))
        self.assertEqual(resolve_fiscal_year(date(2026, 9, 1), self.first), scoped)
        self.assertEqual(resolve_fiscal_year(date(2026, 9, 1), self.second), global_year)
        with self.assertRaises(ValidationError):
            create_fiscal_year(year="Wrong overlap", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30), companies=(self.first,))
        self.assertFalse(FiscalYear.objects.filter(pk="Wrong overlap").exists())
        second_scoped = create_fiscal_year(year="Second 2026", start_date=date(2026, 4, 1), end_date=date(2027, 3, 31), companies=(self.second,))
        self.assertEqual(resolve_fiscal_year(date(2026, 9, 1), self.second), second_scoped)
        with self.assertRaises(ValidationError):
            FiscalYearCompany.objects.create(fiscal_year=scoped, company=self.second)

    def test_posting_uses_year_and_locks_dates(self):
        year = create_fiscal_year(year="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), companies=(self.first,))
        root = Account.objects.create(name="Assets - FI", account_name="Assets", company=self.first, root_type="Asset", is_group=True)
        debit = Account.objects.create(name="Cash - FI", account_name="Cash", company=self.first, parent_account=root)
        credit = Account.objects.create(name="Bank - FI", account_name="Bank", company=self.first, parent_account=root)
        entries = post_gl_entries(
            company=self.first, posting_date=date(2026, 9, 1), voucher_type="Journal Entry", voucher_no="JE-1",
            lines=(LedgerLine(debit, debit=10), LedgerLine(credit, credit=10)),
        )
        self.assertEqual(entries[0].fiscal_year, year)
        self.assertEqual(GLEntry.objects.count(), 2)
        with self.assertRaises(ValidationError):
            year.year_end_date = date(2027, 1, 1)
            year.save()
        with self.assertRaises(ValidationError):
            FiscalYearCompany.objects.get(fiscal_year=year, company=self.first).delete()

    def test_management_command_creates_company_year(self):
        call_command(
            "create_fiscal_year", year="2026-27", start="2026-04-01", end="2027-03-31",
            company=[self.first.name], stdout=StringIO(),
        )
        self.assertEqual(resolve_fiscal_year(date(2026, 9, 1), self.first).year, "2026-27")
        with self.assertRaises(ValidationError):
            resolve_fiscal_year(date(2026, 9, 1), self.second)
