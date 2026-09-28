from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from geo.models import Country, Currency
from organizations.models import Company
from parties.models import Customer, CustomerGroup, Supplier, SupplierGroup

from .models import Account, PartyAccount
from .services import resolve_party_account
from .chart_import import load_chart, plan_chart


class AccountingFoundationTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(name="USD", enabled=True)
        self.eur = Currency.objects.create(name="EUR", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example Inc", abbr="EX", country=country, default_currency=self.usd)
        self.other_company = Company.objects.create(name="Other Inc", abbr="OT", country=country, default_currency=self.eur)
        self.customer = Customer.objects.create(name="CUST-1", customer_name="Customer", default_currency=self.usd)
        self.supplier = Supplier.objects.create(name="SUP-1", supplier_name="Supplier", default_currency=self.usd)
        self.root = Account.objects.create(name="Assets - EX", account_name="Assets", company=self.company, is_group=True, root_type="Asset")
        self.ledger = Account.objects.create(name="Debtors - EX", account_name="Debtors", company=self.company, parent_account=self.root, account_type="Receivable")

    def test_account_tree_supports_multiple_roots_and_reparenting(self):
        second_root = Account.objects.create(name="Liabilities - EX", account_name="Liabilities", company=self.company, is_group=True, root_type="Liability")
        self.root.refresh_from_db()
        self.assertEqual((self.root.lft, self.root.rgt), (1, 4))
        self.assertEqual((second_root.lft, second_root.rgt), (5, 6))
        self.assertEqual((self.ledger.root_type, self.ledger.report_type, self.ledger.account_currency_id), ("Asset", "Balance Sheet", "USD"))
        with self.assertRaises(ValidationError):
            self.root.parent_account = self.ledger
            self.root.save()
        self.root.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.root.is_group = False
            self.root.save()
        self.root.refresh_from_db()
        with self.assertRaises(ValidationError):
            Account.objects.create(name="Wrong child", account_name="Wrong child", company=self.other_company, parent_account=self.root)
        with self.assertRaises(ValidationError):
            Account.objects.create(name="Wrong root", account_name="Wrong root", company=self.company, root_type="Asset")
        with self.assertRaises(ValidationError):
            self.root.delete()
        self.root.account_type = "Current Asset"
        self.root.save()
        self.assertEqual(self.root.account_type, "Current Asset")
        with self.assertRaises(ValidationError):
            self.ledger.is_group = True
            self.ledger.save()

    def test_party_accounts_validate_company_currency_and_uniqueness(self):
        row = PartyAccount.objects.create(customer=self.customer, company=self.company, account=self.ledger)
        self.assertEqual(row.account, self.ledger)
        with self.assertRaises(ValidationError):
            PartyAccount.objects.create(customer=self.customer, company=self.company, account=self.ledger)
        with self.assertRaises(ValidationError):
            PartyAccount.objects.create(customer=self.customer, supplier=self.supplier, company=self.company, account=self.ledger)
        with self.assertRaises(ValidationError):
            PartyAccount.objects.create(supplier=self.supplier, company=self.other_company, account=self.ledger)
        with self.assertRaises(ValidationError):
            PartyAccount.objects.create(supplier=self.supplier, company=self.company, account=self.root)
        euro_account = Account.objects.create(name="Euro - EX", account_name="Euro", company=self.company, parent_account=self.root, account_currency=self.eur)
        with self.assertRaises(ValidationError):
            PartyAccount.objects.create(supplier=self.supplier, company=self.company, account=euro_account)
        with self.assertRaises(ValidationError):
            PartyAccount.objects.create(supplier=self.supplier, company=self.company, account=self.ledger, advance_account=euro_account)
        PartyAccount.objects.create(supplier=self.supplier, company=self.company, account=self.ledger)
        self.assertEqual(PartyAccount.objects.count(), 2)
        with self.assertRaises(ValidationError):
            self.ledger.is_group = True
            self.ledger.save()
        self.ledger.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.ledger.account_currency = self.eur
            self.ledger.save()

    def test_party_group_and_company_account_precedence(self):
        customer_root = CustomerGroup.objects.create(name="All Customer Groups", is_group=True)
        customer_group = CustomerGroup.objects.create(name="Commercial", parent_customer_group=customer_root)
        supplier_root = SupplierGroup.objects.create(name="All Supplier Groups", is_group=True)
        supplier_group = SupplierGroup.objects.create(name="Local", parent_supplier_group=supplier_root)
        self.customer.customer_group = customer_group
        self.customer.save()
        self.supplier.supplier_group = supplier_group
        self.supplier.save()

        company_account = Account.objects.create(name="Company default - EX", account_name="Company default", company=self.company, parent_account=self.root)
        group_account = Account.objects.create(name="Group default - EX", account_name="Group default", company=self.company, parent_account=self.root)
        advance_account = Account.objects.create(name="Advance default - EX", account_name="Advance default", company=self.company, parent_account=self.root)
        self.company.default_receivable_account = company_account
        self.company.default_payable_account = company_account
        self.company.default_advance_received_account = advance_account
        self.company.default_advance_paid_account = advance_account
        self.company.save()
        self.assertEqual(resolve_party_account(self.customer, self.company), company_account)
        self.assertEqual(resolve_party_account(self.supplier, self.company, advance=True), advance_account)

        PartyAccount.objects.create(customer_group=customer_group, company=self.company, account=group_account, advance_account=self.ledger)
        PartyAccount.objects.create(supplier_group=supplier_group, company=self.company, account=group_account, advance_account=self.ledger)
        self.assertEqual(resolve_party_account(self.customer, self.company), group_account)
        self.assertEqual(resolve_party_account(self.supplier, self.company, advance=True), self.ledger)

        PartyAccount.objects.create(customer=self.customer, company=self.company, account=self.ledger)
        PartyAccount.objects.create(supplier=self.supplier, company=self.company, advance_account=advance_account)
        self.assertEqual(resolve_party_account(self.customer, self.company), self.ledger)
        self.assertEqual(resolve_party_account(self.customer, self.company, advance=True), self.ledger)
        self.assertEqual(resolve_party_account(self.supplier, self.company), group_account)
        self.assertEqual(resolve_party_account(self.supplier, self.company, advance=True), advance_account)

        with self.assertRaises(ValidationError):
            PartyAccount.objects.create(customer_group=customer_group, company=self.company, account=group_account)
        with self.assertRaises(ValidationError):
            PartyAccount.objects.create(customer=self.customer, customer_group=customer_group, company=self.company)
        with self.assertRaises(ValidationError):
            self.company.default_receivable_account = self.root
            self.company.save()
        self.company.refresh_from_db()
        with self.assertRaises(ValidationError):
            company_account.disabled = True
            company_account.save()

    def test_group_lookup_uses_direct_group_only(self):
        root = CustomerGroup.objects.create(name="All Customer Groups", is_group=True)
        leaf = CustomerGroup.objects.create(name="Retail", parent_customer_group=root)
        self.customer.customer_group = leaf
        self.customer.save()
        PartyAccount.objects.create(customer_group=root, company=self.company, account=self.ledger)
        self.assertIsNone(resolve_party_account(self.customer, self.company))


class ChartImportTests(TestCase):
    def setUp(self):
        currency = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="New Company", abbr="NC", country=country, default_currency=currency)

    def test_standard_chart_import_and_repeat(self):
        plan = plan_chart(load_chart(template="Standard"), self.company)
        self.assertGreater(len(plan), 50)
        call_command("import_chart_of_accounts", company=self.company.name, verbosity=0)
        self.assertEqual(Account.objects.filter(company=self.company).count(), len(plan))
        self.company.refresh_from_db()
        self.assertEqual(self.company.default_receivable_account.account_type, "Receivable")
        self.assertEqual(self.company.default_payable_account.account_type, "Payable")
        bank_group = Account.objects.get(company=self.company, account_name="Bank Accounts")
        self.assertTrue(bank_group.is_group)
        self.assertEqual(bank_group.account_type, "Bank")
        self.assertGreater(bank_group.rgt, bank_group.lft)
        call_command("import_chart_of_accounts", company=self.company.name, verbosity=0)
        self.assertEqual(Account.objects.filter(company=self.company).count(), len(plan))
        account = Account.objects.filter(company=self.company, is_group=False).first()
        account.account_name = "Custom name"
        account.save()
        with self.assertRaises(CommandError):
            call_command("import_chart_of_accounts", company=self.company.name, verbosity=0)

    def test_numbered_chart_plan_and_reject_existing_chart_without_changes(self):
        plan = plan_chart(load_chart(template="Standard with Numbers"), self.company)
        self.assertGreater(len(plan), 50)
        self.assertTrue(all(row.account_number for row in plan))
        existing = Account.objects.create(name="Manual - NC", account_name="Manual", company=self.company, is_group=True, root_type="Asset")
        with self.assertRaises(CommandError):
            call_command("import_chart_of_accounts", company=self.company.name, template="Standard with Numbers", verbosity=0)
        self.assertEqual(list(Account.objects.filter(company=self.company)), [existing])

    def test_invalid_currency_rolls_back_entire_chart(self):
        plan = plan_chart({
            "Assets": {"root_type": "Asset", "Cash": {"account_currency": "MISSING"}},
        }, self.company)
        with self.assertRaisesRegex(ValueError, "Create these currencies"):
            from .chart_import import install_chart

            install_chart(self.company, plan)
        self.assertFalse(Account.objects.filter(company=self.company).exists())

    def test_json_chart_source_and_parent_structure(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "chart.json"
            source.write_text(json.dumps({"tree": {
                "Assets": {
                    "root_type": "Asset", "Cash Group": {
                        "account_type": "Cash", "Cash": {"account_type": "Cash"},
                    },
                },
            }}), encoding="utf-8")
            call_command("import_chart_of_accounts", company=self.company.name, source=source, verbosity=0)
        root = Account.objects.get(company=self.company, account_name="Assets")
        group = Account.objects.get(company=self.company, account_name="Cash Group")
        cash = Account.objects.get(company=self.company, account_name="Cash")
        self.assertEqual((root.lft, root.rgt), (1, 6))
        self.assertEqual(group.parent_account, root)
        self.assertEqual(cash.parent_account, group)
        self.assertTrue(group.is_group)
