from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from geo.models import Country, Currency
from organizations.models import Company

from .fiscal import create_fiscal_year
from .ledger import LedgerLine, post_gl_entries
from .models import Account, AccountingPeriod, ClosedDocument, GLEntry
from .periods import PERIOD_CLOSING_DOCUMENT_TYPES, create_accounting_period, validate_accounting_period


class AccountingPeriodTests(TestCase):
    def setUp(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=usd)
        self.other = Company.objects.create(name="Other", abbr="OT", country=country, default_currency=usd)
        self.start = date(2025, 4, 1)
        self.end = date(2025, 6, 30)
        create_fiscal_year(year="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))
        root = Account.objects.create(name="Assets - EX", account_name="Assets", company=self.company, root_type="Asset", is_group=True)
        self.bank = Account.objects.create(name="Bank - EX", account_name="Bank", company=self.company, parent_account=root)
        self.cash = Account.objects.create(name="Cash - EX", account_name="Cash", company=self.company, parent_account=root)

    def post(self, *, document_type="Journal Entry", posting_date=date(2025, 5, 1), user=None, number="JE-1"):
        return post_gl_entries(
            company=self.company, posting_date=posting_date, voucher_type=document_type,
            voucher_no=number, user=user,
            lines=(LedgerLine(self.bank, debit=10), LedgerLine(self.cash, credit=10)),
        )

    def test_dates_overlap_scope_and_default_rows(self):
        period = create_accounting_period(
            period_name="Q2 2025", company=self.company, start_date=self.start, end_date=self.end,
        )
        self.assertEqual(period.name, "Q2 2025 - EX")
        self.assertEqual(set(period.closed_documents.values_list("document_type", flat=True)), set(PERIOD_CLOSING_DOCUMENT_TYPES))
        self.assertEqual(period.closed_documents.filter(closed=True).count(), len(PERIOD_CLOSING_DOCUMENT_TYPES))
        with self.assertRaises(ValidationError):
            create_accounting_period(period_name="Overlap", company=self.company, start_date=self.end, end_date=self.end)
        with self.assertRaises(ValidationError):
            create_accounting_period(period_name="Reversed", company=self.company, start_date=self.end, end_date=self.start)
        with self.assertRaises(ValidationError):
            create_accounting_period(period_name="Future", company=self.company, start_date=timezone.localdate(), end_date=timezone.localdate() + timedelta(days=1))
        self.assertFalse(AccountingPeriod.objects.filter(period_name__in=("Overlap", "Reversed", "Future")).exists())
        other = create_accounting_period(period_name="Other Q2", company=self.other, start_date=self.start, end_date=self.end)
        self.assertEqual(other.company, self.other)

    def test_closed_document_gates_posting_by_type_date_and_role(self):
        role = Group.objects.create(name="Accounts Manager")
        period = create_accounting_period(
            period_name="Closed Q2", company=self.company, start_date=self.start, end_date=self.end,
            exempted_role=role, closed_document_types=("Journal Entry", "Sales Invoice"),
        )
        with self.assertRaises(ValidationError):
            self.post()
        self.assertFalse(GLEntry.objects.exists())
        self.post(document_type="Purchase Invoice", number="PI-1")
        self.post(document_type="Journal Entry", posting_date=date(2025, 7, 1), number="JE-2")
        user = get_user_model().objects.create_user(username="accountant", password="test-password")
        with self.assertRaises(ValidationError):
            self.post(user=user, number="JE-3")
        user.groups.add(role)
        self.post(user=user, number="JE-3")
        self.assertEqual(GLEntry.objects.count(), 6)
        period.closed_documents.filter(document_type="Journal Entry").update(closed=False)
        self.post(number="JE-4")
        period.closed_documents.filter(document_type="Journal Entry").update(closed=True)
        period.disabled = True
        period.save()
        self.post(number="JE-5")

    def test_bank_clearance_exception_and_atomic_invalid_rows(self):
        period = create_accounting_period(
            period_name="Q2", company=self.company, start_date=self.start, end_date=self.end,
        )
        validate_accounting_period(company=self.company, posting_date=date(2025, 5, 1), document_type="Bank Clearance")
        self.post(document_type="Bank Clearance", number="BC-1")
        with self.assertRaises(ValidationError):
            create_accounting_period(
                period_name="Bad docs", company=self.other, start_date=self.start, end_date=self.end,
                closed_document_types=("Journal Entry", "Journal Entry"),
            )
        self.assertFalse(AccountingPeriod.objects.filter(period_name="Bad docs").exists())
        with self.assertRaises(ValidationError):
            ClosedDocument.objects.create(accounting_period=period, document_type="Unknown", closed=True)
