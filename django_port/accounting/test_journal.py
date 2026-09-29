from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from geo.models import Country, Currency, CurrencyExchange
from organizations.models import Company
from parties.models import Customer
from projects.models import Project

from .fiscal import create_fiscal_year
from .journal import submit_journal_entry
from .models import Account, CostCenter, FinanceBook, GLEntry, JournalEntry, JournalEntryAccount
from .periods import create_accounting_period


class JournalEntryTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=self.usd)
        create_fiscal_year(year="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))
        assets = Account.objects.create(name="Assets - EX", account_name="Assets", company=self.company, root_type="Asset", is_group=True)
        self.bank = Account.objects.create(name="Bank - EX", account_name="Bank", company=self.company, parent_account=assets)
        self.cash = Account.objects.create(name="Cash - EX", account_name="Cash", company=self.company, parent_account=assets)
        income = Account.objects.create(name="Income - EX", account_name="Income", company=self.company, root_type="Income", is_group=True)
        self.sales = Account.objects.create(name="Sales - EX", account_name="Sales", company=self.company, parent_account=income)
        root = CostCenter.objects.create(name="Example - EX", cost_center_name="Example", company=self.company, is_group=True)
        self.center = CostCenter.objects.create(name="Main - EX", cost_center_name="Main", company=self.company, parent_cost_center=root)

    def journal(self, *, name="JE-1", voucher_type="Journal Entry", multi_currency=False, finance_book=None):
        return JournalEntry.objects.create(
            name=name, company=self.company, posting_date=date(2025, 5, 1),
            voucher_type=voucher_type, multi_currency=multi_currency,
            finance_book=finance_book, remark="Test journal",
        )

    def row(self, journal, position, account, *, debit=0, credit=0, cost_center=None, customer=None, exchange_rate=None,
            reference_type="", reference_name="", project=None):
        return JournalEntryAccount.objects.create(
            journal_entry=journal, position=position, account=account, cost_center=cost_center, project=project,
            customer=customer, debit_in_account_currency=Decimal(str(debit)),
            credit_in_account_currency=Decimal(str(credit)), exchange_rate=exchange_rate,
            reference_type=reference_type, reference_name=reference_name,
        )

    def test_reference_fields_reach_gl_and_require_a_pair(self):
        journal = self.journal()
        with self.assertRaises(ValidationError):
            self.row(journal, 1, self.bank, debit=10, reference_type="Sales Invoice")
        self.row(journal, 1, self.bank, debit=10, reference_type="Sales Invoice", reference_name="SI-1")
        self.row(journal, 2, self.cash, credit=10)
        submit_journal_entry(journal)
        linked = GLEntry.objects.get(voucher_no=journal.name, account=self.bank)
        self.assertEqual((linked.against_voucher_type, linked.against_voucher), ("Sales Invoice", "SI-1"))
        self.assertEqual(GLEntry.objects.get(voucher_no=journal.name, account=self.cash).against_voucher, "")

    def test_project_is_propagated_and_must_match_company(self):
        project = Project.objects.create(name="PROJ-1", project_name="Website", company=self.company)
        other = Company.objects.create(name="Other", abbr="OT", country=self.company.country,
                                       default_currency=self.usd)
        foreign_project = Project.objects.create(name="PROJ-2", project_name="Other Website", company=other)
        journal = self.journal()
        with self.assertRaises(ValidationError):
            self.row(journal, 1, self.bank, debit=10, project=foreign_project)
        self.row(journal, 1, self.bank, debit=10, project=project)
        self.row(journal, 2, self.cash, credit=10)
        submit_journal_entry(journal)
        self.assertEqual(GLEntry.objects.get(voucher_no=journal.name, account=self.bank).project_id, project.pk)
        self.assertIsNone(GLEntry.objects.get(voucher_no=journal.name, account=self.cash).project_id)

    def test_balanced_submission_and_immutable_rows(self):
        journal = self.journal()
        first = self.row(journal, 1, self.bank, debit=125)
        self.row(journal, 2, self.sales, credit=125, cost_center=self.center)
        posted = submit_journal_entry(journal)
        self.assertEqual(posted.status, JournalEntry.Status.SUBMITTED)
        self.assertEqual((posted.total_debit, posted.total_credit), (Decimal("125"), Decimal("125")))
        self.assertEqual(GLEntry.objects.filter(voucher_type="Journal Entry", voucher_no=journal.name).count(), 2)
        with self.assertRaises(ValidationError):
            submit_journal_entry(journal)
        with self.assertRaises(ValidationError):
            first.delete()
        first.user_remark = "Changed after submission"
        with self.assertRaises(ValidationError):
            first.save()
        with self.assertRaises(ValidationError):
            JournalEntryAccount.objects.filter(pk=first.pk).delete()
        with self.assertRaises(ValidationError):
            journal.delete()
        with self.assertRaises(ValidationError):
            JournalEntry.objects.filter(pk=journal.pk).update(status="Draft")

    def test_unbalanced_or_missing_party_rolls_back(self):
        journal = self.journal()
        self.row(journal, 1, self.bank, debit=100)
        self.row(journal, 2, self.sales, credit=90, cost_center=self.center)
        with self.assertRaises(ValidationError):
            submit_journal_entry(journal)
        self.assertFalse(GLEntry.objects.exists())
        journal.refresh_from_db()
        self.assertEqual(journal.status, JournalEntry.Status.DRAFT)
        receivable = Account.objects.create(
            name="Receivable - EX", account_name="Receivable", company=self.company,
            parent_account=self.bank.parent_account, account_type="Receivable",
        )
        second = journal.accounts.get(position=2)
        second.account = receivable
        second.credit_in_account_currency = Decimal("100")
        second.cost_center = None
        second.save()
        with self.assertRaises(ValidationError):
            submit_journal_entry(journal)
        customer = Customer.objects.create(name="CUST-1", customer_name="Customer")
        second.customer = customer
        second.save()
        submit_journal_entry(journal)
        self.assertEqual(GLEntry.objects.count(), 2)

    def test_foreign_account_requires_multi_currency_and_recorded_rate(self):
        eur = Currency.objects.create(name="EUR", enabled=True)
        euro_bank = Account.objects.create(
            name="Euro Bank - EX", account_name="Euro Bank", company=self.company,
            parent_account=self.bank.parent_account, account_currency=eur,
        )
        journal = self.journal()
        self.row(journal, 1, euro_bank, debit=100)
        self.row(journal, 2, self.bank, credit=120)
        with self.assertRaises(ValidationError):
            submit_journal_entry(journal)
        journal.multi_currency = True
        journal.save()
        with self.assertRaises(ValidationError):
            submit_journal_entry(journal)
        CurrencyExchange.objects.create(
            date=journal.posting_date, from_currency=eur, to_currency=self.usd,
            exchange_rate=Decimal("1.2"),
        )
        posted = submit_journal_entry(journal)
        self.assertEqual(posted.total_debit, Decimal("120"))
        foreign = GLEntry.objects.get(voucher_no=journal.name, account=euro_bank)
        self.assertEqual((foreign.debit, foreign.debit_in_account_currency, foreign.account_exchange_rate),
                         (Decimal("120"), Decimal("100"), Decimal("1.2")))

    def test_opening_entry_uses_journal_voucher_and_opening_flag(self):
        journal = self.journal(voucher_type="Opening Entry")
        self.row(journal, 1, self.bank, debit=50)
        self.row(journal, 2, self.cash, credit=50)
        submit_journal_entry(journal)
        self.assertEqual(GLEntry.objects.filter(voucher_type="Journal Entry", is_opening=True).count(), 2)

    def test_closed_accounting_period_blocks_journal_submission(self):
        journal = self.journal()
        self.row(journal, 1, self.bank, debit=10)
        self.row(journal, 2, self.cash, credit=10)
        create_accounting_period(
            period_name="Closed May", company=self.company,
            start_date=date(2025, 5, 1), end_date=date(2025, 5, 31),
            closed_document_types=("Journal Entry",),
        )
        with self.assertRaises(ValidationError):
            submit_journal_entry(journal)
        self.assertFalse(GLEntry.objects.exists())
        journal.refresh_from_db()
        self.assertEqual(journal.status, JournalEntry.Status.DRAFT)

    def test_finance_book_is_global_and_propagates_to_gl(self):
        book = FinanceBook.objects.create(finance_book_name="Statutory")
        self.assertEqual(book.name, "Statutory")
        self.company.default_finance_book = book
        self.company.save()
        journal = self.journal(finance_book=book)
        self.row(journal, 1, self.bank, debit=10)
        self.row(journal, 2, self.cash, credit=10)
        submit_journal_entry(journal)
        self.assertEqual(set(GLEntry.objects.filter(voucher_no=journal.name).values_list("finance_book_id", flat=True)), {book.pk})
        with self.assertRaises(ValidationError):
            book.finance_book_name = "Renamed"
            book.save()
