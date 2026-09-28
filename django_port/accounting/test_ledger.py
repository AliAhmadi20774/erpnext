from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from geo.models import Country, Currency, CurrencyExchange
from organizations.models import Company
from parties.models import Customer, Supplier

from .ledger import LedgerLine, account_balance, post_gl_entries
from .models import Account, GLEntry
from .fiscal import create_fiscal_year


class LedgerPostingTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(name="USD", enabled=True)
        self.eur = Currency.objects.create(name="EUR", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=self.usd)
        self.other_company = Company.objects.create(name="Other", abbr="OT", country=country, default_currency=self.usd)
        self.customer = Customer.objects.create(name="CUST-1", customer_name="Customer")
        self.supplier = Supplier.objects.create(name="SUP-1", supplier_name="Supplier")
        self.assets = Account.objects.create(name="Assets - EX", account_name="Assets", company=self.company, root_type="Asset", is_group=True)
        self.bank = Account.objects.create(name="Bank - EX", account_name="Bank", company=self.company, parent_account=self.assets)
        self.receivable = Account.objects.create(name="Receivable - EX", account_name="Receivable", company=self.company, parent_account=self.assets, account_type="Receivable")
        self.other_root = Account.objects.create(name="Assets - OT", account_name="Assets", company=self.other_company, root_type="Asset", is_group=True)
        self.other_bank = Account.objects.create(name="Bank - OT", account_name="Bank", company=self.other_company, parent_account=self.other_root)
        create_fiscal_year(year="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))

    def post(self, lines, number="OPEN-1"):
        return post_gl_entries(
            company=self.company, posting_date=date(2026, 9, 1),
            voucher_type="Opening Entry", voucher_no=number,
            lines=lines, is_opening=True,
        )

    def test_balanced_voucher_and_balance(self):
        entries = self.post([
            LedgerLine(self.bank, debit=Decimal("125.50")),
            LedgerLine(self.receivable, credit=Decimal("125.50"), customer=self.customer),
        ])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].fiscal_year_id, "2026")
        self.assertEqual((entries[0].debit_in_account_currency, entries[1].credit_in_account_currency), (Decimal("125.50"), Decimal("125.50")))
        self.assertEqual(account_balance(self.bank), Decimal("125.50"))
        self.assertEqual(account_balance(self.receivable, as_of=date(2026, 8, 31)), Decimal("0"))
        self.assertEqual(account_balance(self.receivable), Decimal("-125.50"))
        with self.assertRaises(ValidationError):
            self.post([LedgerLine(self.bank, debit=1), LedgerLine(self.receivable, credit=1, customer=self.customer)])
        with self.assertRaises(ValidationError):
            entries[0].save()
        with self.assertRaises(ValidationError):
            entries[0].delete()
        with self.assertRaises(ValidationError):
            GLEntry.objects.filter(pk=entries[0].pk).update(debit=0)
        with self.assertRaises(ValidationError):
            GLEntry.objects.filter(pk=entries[0].pk).delete()
        self.assertEqual(GLEntry.objects.count(), 2)
        with self.assertRaises(ValidationError):
            self.bank.account_currency = self.eur
            self.bank.save()

    def test_unbalanced_or_invalid_line_leaves_no_entries(self):
        bad_batches = [
            [LedgerLine(self.bank, debit=2), LedgerLine(self.receivable, credit=1, customer=self.customer)],
            [LedgerLine(self.bank, debit=1), LedgerLine(self.receivable, credit=1)],
            [LedgerLine(self.bank, debit=1), LedgerLine(self.other_bank, credit=1)],
            [LedgerLine(self.assets, debit=1), LedgerLine(self.bank, credit=1)],
            [LedgerLine(self.bank, debit=1, credit=1), LedgerLine(self.receivable, credit=1, customer=self.customer)],
            [LedgerLine(self.bank, debit=-1), LedgerLine(self.receivable, credit=1, customer=self.customer)],
            [LedgerLine(self.bank, debit=1)],
        ]
        for index, lines in enumerate(bad_batches):
            with self.subTest(index=index), self.assertRaises(ValidationError):
                self.post(lines, number=f"BAD-{index}")
            self.assertEqual(GLEntry.objects.count(), 0)

    def test_profit_loss_and_foreign_currency_wait_for_dependencies(self):
        income_root = Account.objects.create(name="Income - EX", account_name="Income", company=self.company, root_type="Income", is_group=True)
        sales = Account.objects.create(name="Sales - EX", account_name="Sales", company=self.company, parent_account=income_root)
        euro_bank = Account.objects.create(name="Euro Bank - EX", account_name="Euro Bank", company=self.company, parent_account=self.assets, account_currency=self.eur)
        for blocked in (sales, euro_bank):
            with self.assertRaises(ValidationError):
                self.post([LedgerLine(self.bank, debit=1), LedgerLine(blocked, credit=1)], number=blocked.name)
        self.assertEqual(GLEntry.objects.count(), 0)

    def test_payable_requires_supplier_and_disabled_party_is_rejected(self):
        liabilities = Account.objects.create(name="Liabilities - EX", account_name="Liabilities", company=self.company, root_type="Liability", is_group=True)
        payable = Account.objects.create(name="Payable - EX", account_name="Payable", company=self.company, parent_account=liabilities, account_type="Payable")
        with self.assertRaises(ValidationError):
            self.post([LedgerLine(self.bank, debit=1), LedgerLine(payable, credit=1, customer=self.customer)])
        self.supplier.disabled = True
        self.supplier.save()
        with self.assertRaises(ValidationError):
            self.post([LedgerLine(self.bank, debit=1), LedgerLine(payable, credit=1, supplier=self.supplier)])
        self.supplier.disabled = False
        self.supplier.save()
        self.post([LedgerLine(self.bank, debit=1), LedgerLine(payable, credit=1, supplier=self.supplier)])
        self.assertEqual(account_balance(payable), Decimal("-1"))

    def test_foreign_account_uses_recorded_rate_and_preserves_source_amount(self):
        euro_bank = Account.objects.create(name="Euro Bank - EX", account_name="Euro Bank", company=self.company, parent_account=self.assets, account_currency=self.eur)
        CurrencyExchange.objects.create(
            date=date(2026, 9, 1), from_currency=self.eur, to_currency=self.usd,
            exchange_rate=Decimal("1.250000000"),
        )
        entries = self.post([
            LedgerLine(euro_bank, debit_in_account_currency=Decimal("100")),
            LedgerLine(self.bank, credit=Decimal("125")),
        ])
        self.assertEqual((entries[0].debit, entries[0].debit_in_account_currency, entries[0].account_exchange_rate),
                         (Decimal("125.000000000"), Decimal("100"), Decimal("1.250000000")))
        self.assertEqual(account_balance(euro_bank), Decimal("125"))
        with self.assertRaises(ValidationError):
            euro_bank.account_currency = self.usd
            euro_bank.save()

    def test_foreign_posting_requires_rate_and_balances_base_currency(self):
        euro_bank = Account.objects.create(name="Euro Bank - EX", account_name="Euro Bank", company=self.company, parent_account=self.assets, account_currency=self.eur)
        with self.assertRaises(ValidationError):
            self.post([LedgerLine(euro_bank, debit_in_account_currency=100), LedgerLine(self.bank, credit=125)])
        with self.assertRaises(ValidationError):
            self.post([LedgerLine(euro_bank, debit_in_account_currency=100, exchange_rate=Decimal("1.2")), LedgerLine(self.bank, credit=125)])
        with self.assertRaises(ValidationError):
            self.post([LedgerLine(euro_bank, debit=125, debit_in_account_currency=100, exchange_rate=Decimal("1.25")), LedgerLine(self.bank, credit=125)])
        self.assertEqual(GLEntry.objects.count(), 0)
        self.post([LedgerLine(euro_bank, debit_in_account_currency=100, exchange_rate=Decimal("1.25")), LedgerLine(self.bank, credit=125)])
        self.assertEqual(GLEntry.objects.count(), 2)
