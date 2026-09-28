from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from geo.models import Country, Currency
from organizations.models import Company

from .ledger import LedgerLine, account_balance, post_gl_entries
from .models import Account, CostCenter, GLEntry
from .fiscal import create_fiscal_year


class CostCenterTests(TestCase):
    def setUp(self):
        currency = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=currency)
        self.other_company = Company.objects.create(name="Other", abbr="OT", country=country, default_currency=currency)
        create_fiscal_year(year="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))

    def test_seed_command_is_idempotent_and_sets_default(self):
        call_command("seed_company_cost_centers", company=self.company.name, verbosity=0)
        call_command("seed_company_cost_centers", company=self.company.name, verbosity=0)
        self.company.refresh_from_db()
        self.assertEqual(CostCenter.objects.filter(company=self.company).count(), 2)
        root = CostCenter.objects.get(company=self.company, parent_cost_center__isnull=True)
        main = self.company.cost_center
        self.assertEqual((root.name, root.lft, root.rgt), ("Example - EX", 1, 4))
        self.assertEqual((main.name, main.parent_cost_center, main.lft, main.rgt), ("Main - EX", root, 2, 3))
        with self.assertRaises(ValidationError):
            root.delete()

    def test_tree_rejects_cycle_foreign_parent_and_group_change(self):
        root = CostCenter.objects.create(company=self.company, cost_center_name="Example", is_group=True)
        branch = CostCenter.objects.create(company=self.company, cost_center_name="Operations", parent_cost_center=root, is_group=True)
        leaf = CostCenter.objects.create(company=self.company, cost_center_name="Sales", parent_cost_center=branch)
        root.refresh_from_db()
        self.assertEqual((root.lft, root.rgt), (1, 6))
        with self.assertRaises(ValidationError):
            root.parent_cost_center = branch
            root.save()
        root.refresh_from_db()
        with self.assertRaises(ValidationError):
            branch.is_group = False
            branch.save()
        branch.refresh_from_db()
        with self.assertRaises(ValidationError):
            CostCenter.objects.create(company=self.company, cost_center_name="Example", is_group=True)
        other_root = CostCenter.objects.create(company=self.other_company, cost_center_name="Other", is_group=True)
        with self.assertRaises(ValidationError):
            CostCenter.objects.create(company=self.company, cost_center_name="Wrong", parent_cost_center=other_root)
        self.assertEqual(leaf.parent_cost_center, branch)

    def test_profit_loss_posting_requires_valid_cost_center(self):
        call_command("seed_company_cost_centers", company=self.company.name, verbosity=0)
        self.company.refresh_from_db()
        root = CostCenter.objects.get(company=self.company, parent_cost_center__isnull=True)
        main = self.company.cost_center
        assets = Account.objects.create(name="Assets - EX", account_name="Assets", company=self.company, root_type="Asset", is_group=True)
        bank = Account.objects.create(name="Bank - EX", account_name="Bank", company=self.company, parent_account=assets)
        expense_root = Account.objects.create(name="Expenses - EX", account_name="Expenses", company=self.company, root_type="Expense", is_group=True)
        expense = Account.objects.create(name="Rent - EX", account_name="Rent", company=self.company, parent_account=expense_root)

        def post(center, number, opening=False):
            return post_gl_entries(
                company=self.company, posting_date=date(2026, 9, 2),
                voucher_type="Journal Entry", voucher_no=number, is_opening=opening,
                lines=[LedgerLine(expense, debit=Decimal("12"), cost_center=center), LedgerLine(bank, credit=Decimal("12"))],
            )

        with self.assertRaises(ValidationError):
            post(None, "NO-CENTER")
        with self.assertRaises(ValidationError):
            post(root, "GROUP")
        other_root = CostCenter.objects.create(company=self.other_company, cost_center_name="Other", is_group=True)
        other_main = CostCenter.objects.create(company=self.other_company, cost_center_name="Main", parent_cost_center=other_root)
        with self.assertRaises(ValidationError):
            post(other_main, "FOREIGN")
        with self.assertRaises(ValidationError):
            post(main, "OPENING", opening=True)
        self.assertFalse(GLEntry.objects.exists())

        entries = post(main, "JE-1")
        self.assertEqual(entries[0].cost_center, main)
        self.assertEqual(account_balance(expense), Decimal("12"))
        with self.assertRaises(ValidationError):
            main.disabled = True
            main.save()
        main.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.company.cost_center = root
            self.company.save()
