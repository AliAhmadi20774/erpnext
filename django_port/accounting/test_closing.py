from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from geo.models import Country, Currency
from organizations.models import Company

from .closing import submit_period_closing_voucher
from .fiscal import create_fiscal_year
from .ledger import LedgerLine, account_balance, post_gl_entries
from .models import Account, CostCenter, GLEntry, PeriodClosingVoucher
from .periods import create_accounting_period


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

    def post_activity(self, *, date_value=date(2025, 2, 1), suffix="1"):
        post_gl_entries(
            company=self.company, posting_date=date_value, voucher_type="Journal Entry", voucher_no=f"SALES-{suffix}",
            lines=(LedgerLine(self.bank, debit=100), LedgerLine(self.sales, credit=100, cost_center=self.center)),
        )
        post_gl_entries(
            company=self.company, posting_date=date_value, voucher_type="Journal Entry", voucher_no=f"RENT-{suffix}",
            lines=(LedgerLine(self.rent, debit=30, cost_center=self.center), LedgerLine(self.bank, credit=30)),
        )

    def test_closing_posts_balanced_reversals_and_continues_next_period(self):
        self.post_activity()
        first = submit_period_closing_voucher(self.voucher())
        self.assertEqual(first.status, PeriodClosingVoucher.Status.SUBMITTED)
        self.assertEqual(GLEntry.objects.filter(voucher_type="Period Closing Voucher", voucher_no=first.name).count(), 3)
        self.assertEqual(account_balance(self.sales), Decimal("0"))
        self.assertEqual(account_balance(self.rent), Decimal("0"))
        self.assertEqual(account_balance(self.retained), Decimal("-70"))
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
        voucher = submit_period_closing_voucher(self.voucher())
        self.assertEqual(voucher.status, PeriodClosingVoucher.Status.SUBMITTED)
        self.assertFalse(GLEntry.objects.filter(voucher_type="Period Closing Voucher").exists())

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
