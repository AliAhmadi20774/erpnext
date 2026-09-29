from io import StringIO
from datetime import date, time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounting.models import Account, FiscalYear
from catalog.models import Item, ItemGroup, UnitOfMeasure
from geo.models import Country, Currency
from organizations.models import Company

from .ledger import StockLedgerLine, post_stock_entries
from .models import Bin, StockLedgerEntry, Warehouse, WarehouseType


class WarehouseModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Acme Company", abbr="AC", country=country, default_currency=currency
        )
        cls.asset_root = Account.objects.create(
            name="Assets - AC",
            account_name="Assets",
            company=cls.company,
            is_group=True,
            root_type="Asset",
            account_currency=currency,
        )
        cls.stock_account = Account.objects.create(
            name="Stock - AC",
            account_name="Stock",
            company=cls.company,
            parent_account=cls.asset_root,
            account_type="Stock",
            account_currency=currency,
        )
        cls.company.default_inventory_account = cls.stock_account
        cls.company.save(update_fields=("default_inventory_account",))

    def make_warehouse(self, name, **kwargs):
        values = {"warehouse_name": name, "company": self.company}
        values.update(kwargs)
        return Warehouse.objects.create(**values)

    def test_warehouse_type_requires_a_name(self):
        warehouse_type = WarehouseType(name="  Cold Storage  ")
        warehouse_type.save()
        self.assertEqual(warehouse_type.name, "Cold Storage")
        warehouse_type.name = "Renamed"
        with self.assertRaises(ValidationError):
            warehouse_type.save()
        with self.assertRaises(ValidationError):
            WarehouseType(name="  ").save()

    def test_naming_tree_bounds_descendants_and_multiple_roots(self):
        root = self.make_warehouse("All Warehouses", is_group=True)
        stores = self.make_warehouse("Stores", parent_warehouse=root)
        standalone = self.make_warehouse("Standalone - AC")

        self.assertEqual(root.name, "All Warehouses - AC")
        self.assertEqual(stores.name, "Stores - AC")
        self.assertEqual(standalone.name, "Standalone - AC")
        root.refresh_from_db()
        stores.refresh_from_db()
        standalone.refresh_from_db()
        self.assertLess(root.lft, stores.lft)
        self.assertGreater(root.rgt, stores.rgt)
        self.assertEqual(list(root.descendants()), [stores])
        self.assertEqual(list(root.descendants(include_self=True)), [root, stores])
        self.assertLess(root.rgt, standalone.lft)

    def test_moving_a_warehouse_rebuilds_the_tree(self):
        root = self.make_warehouse("All Warehouses", is_group=True)
        first = self.make_warehouse("First", parent_warehouse=root, is_group=True)
        second = self.make_warehouse("Second", parent_warehouse=root, is_group=True)
        leaf = self.make_warehouse("Leaf", parent_warehouse=first)

        leaf.parent_warehouse = second
        leaf.save()
        first.refresh_from_db()
        second.refresh_from_db()
        leaf.refresh_from_db()
        self.assertFalse(first.descendants().exists())
        self.assertEqual(list(second.descendants()), [leaf])

    def test_parent_must_be_a_group_in_the_same_company_and_tree_cannot_cycle(self):
        root = self.make_warehouse("Root", is_group=True)
        leaf = self.make_warehouse("Leaf", parent_warehouse=root)
        with self.assertRaises(ValidationError):
            self.make_warehouse("Bad Parent", parent_warehouse=leaf)

        other = Company.objects.create(
            name="Other Company",
            abbr="OC",
            country=self.company.country,
            default_currency=self.company.default_currency,
            enable_perpetual_inventory=False,
        )
        other_root = Warehouse.objects.create(
            warehouse_name="Other Root", company=other, is_group=True
        )
        with self.assertRaises(ValidationError):
            self.make_warehouse("Cross Company", parent_warehouse=other_root)

        child_group = self.make_warehouse("Child Group", parent_warehouse=root, is_group=True)
        root.parent_warehouse = child_group
        with self.assertRaises(ValidationError):
            root.save()

    def test_group_with_children_cannot_become_a_leaf_or_be_deleted(self):
        root = self.make_warehouse("Root", is_group=True)
        self.make_warehouse("Leaf", parent_warehouse=root)
        root.is_group = False
        with self.assertRaises(ValidationError):
            root.save()
        with self.assertRaises(ValidationError):
            root.delete()

    def test_company_and_names_are_immutable_without_a_rename_workflow(self):
        warehouse = self.make_warehouse("Stores")
        warehouse.warehouse_name = "Renamed"
        with self.assertRaises(ValidationError):
            warehouse.save()

        warehouse.refresh_from_db()
        warehouse.name = "Forged - AC"
        with self.assertRaises(ValidationError):
            warehouse.save()

        other = Company.objects.create(
            name="Other Company",
            abbr="OC",
            country=self.company.country,
            default_currency=self.company.default_currency,
            enable_perpetual_inventory=False,
        )
        warehouse = Warehouse.objects.get(pk="Stores - AC")
        warehouse.company = other
        with self.assertRaises(ValidationError):
            warehouse.save()

    def test_account_must_be_an_enabled_stock_ledger_for_the_company(self):
        expense_root = Account.objects.create(
            name="Expenses - AC",
            account_name="Expenses",
            company=self.company,
            is_group=True,
            root_type="Expense",
            account_currency=self.company.default_currency,
        )
        expense = Account.objects.create(
            name="Expense - AC",
            account_name="Expense",
            company=self.company,
            parent_account=expense_root,
            account_type="Expense Account",
            account_currency=self.company.default_currency,
        )
        for account in (self.asset_root, expense):
            with self.subTest(account=account.name), self.assertRaises(ValidationError):
                self.make_warehouse(f"Bad {account.name}", account=account)

        other = Company.objects.create(
            name="Other Company",
            abbr="OC",
            country=self.company.country,
            default_currency=self.company.default_currency,
        )
        other_root = Account.objects.create(
            name="Assets - OC",
            account_name="Assets",
            company=other,
            is_group=True,
            root_type="Asset",
            account_currency=self.company.default_currency,
        )
        other_stock = Account.objects.create(
            name="Stock - OC",
            account_name="Stock",
            company=other,
            parent_account=other_root,
            account_type="Stock",
            account_currency=self.company.default_currency,
        )
        with self.assertRaises(ValidationError):
            self.make_warehouse("Foreign Account", account=other_stock)

    def test_effective_account_uses_ancestor_company_default_and_single_stock_fallback(self):
        root = self.make_warehouse("Root", is_group=True, account=self.stock_account)
        child = self.make_warehouse("Child", parent_warehouse=root)
        self.assertEqual(child.effective_account(), self.stock_account)

        root.account = None
        root.save()
        child.refresh_from_db()
        self.assertEqual(child.effective_account(), self.stock_account)

        self.company.default_inventory_account = None
        self.company.save(update_fields=("default_inventory_account",))
        child.refresh_from_db()
        self.assertEqual(child.effective_account(), self.stock_account)

        Account.objects.create(
            name="Second Stock - AC",
            account_name="Second Stock",
            company=self.company,
            parent_account=self.asset_root,
            account_type="Stock",
            account_currency=self.company.default_currency,
        )
        with self.assertRaises(ValidationError):
            self.make_warehouse("Ambiguous")

    def test_transit_warehouse_rules(self):
        transit_type = WarehouseType.objects.create(name="Transit")
        normal_type = WarehouseType.objects.create(name="Normal")
        root = self.make_warehouse("Root", is_group=True)
        transit = self.make_warehouse(
            "Transit", parent_warehouse=root, warehouse_type=transit_type
        )
        warehouse = self.make_warehouse(
            "Stores", parent_warehouse=root, default_in_transit_warehouse=transit
        )
        self.assertEqual(warehouse.default_in_transit_warehouse, transit)

        normal = self.make_warehouse("Normal", parent_warehouse=root, warehouse_type=normal_type)
        warehouse.default_in_transit_warehouse = normal
        with self.assertRaises(ValidationError):
            warehouse.save()
        root.warehouse_type = transit_type
        with self.assertRaises(ValidationError):
            root.save()

    def test_company_defaults_and_reverse_account_references_are_protected(self):
        transit_type = WarehouseType.objects.create(name="Transit")
        stores = self.make_warehouse("Stores")
        transit = self.make_warehouse("Transit", warehouse_type=transit_type)
        self.company.default_warehouse = stores
        self.company.default_in_transit_warehouse = transit
        self.company.save()

        stores.disabled = True
        with self.assertRaises(ValidationError):
            stores.save()
        transit.warehouse_type = None
        with self.assertRaises(ValidationError):
            transit.save()

        self.stock_account.account_type = "Current Asset"
        with self.assertRaises(ValidationError):
            self.stock_account.save()
        self.stock_account.refresh_from_db()

        account_warehouse = self.make_warehouse("Account Warehouse", account=self.stock_account)
        self.assertEqual(account_warehouse.account, self.stock_account)
        self.stock_account.account_type = "Current Asset"
        with self.assertRaises(ValidationError):
            self.stock_account.save()

    def test_validated_fields_cannot_be_changed_with_queryset_update(self):
        warehouse = self.make_warehouse("Stores")
        with self.assertRaises(ValidationError):
            Warehouse.objects.filter(pk=warehouse.pk).update(disabled=True)


class DefaultWarehouseCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Seed Company",
            abbr="SC",
            country=country,
            default_currency=currency,
        )

    def test_command_builds_erpnext_defaults_and_is_idempotent(self):
        output = StringIO()
        call_command("seed_company_warehouses", company=self.company.pk, stdout=output)
        call_command("seed_company_warehouses", company=self.company.pk, stdout=output)

        self.assertEqual(Warehouse.objects.filter(company=self.company).count(), 5)
        root = Warehouse.objects.get(company=self.company, warehouse_name="All Warehouses")
        self.assertTrue(root.is_group)
        self.assertEqual(root.children.count(), 4)
        self.company.refresh_from_db()
        self.assertEqual(self.company.default_warehouse.warehouse_name, "Stores")
        self.assertEqual(
            self.company.default_in_transit_warehouse.warehouse_name, "Goods In Transit"
        )
        self.assertEqual(self.company.default_in_transit_warehouse.warehouse_type_id, "Transit")

    def test_command_rejects_group_company(self):
        self.company.is_group = True
        self.company.save()
        with self.assertRaises(CommandError):
            call_command("seed_company_warehouses", company=self.company.pk)


class StockLedgerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Ledger Company", abbr="LC", country=country, default_currency=currency
        )
        asset_root = Account.objects.create(
            name="Assets - LC",
            account_name="Assets",
            company=cls.company,
            is_group=True,
            root_type="Asset",
            account_currency=currency,
        )
        cls.stock_account = Account.objects.create(
            name="Stock - LC",
            account_name="Stock",
            company=cls.company,
            parent_account=asset_root,
            account_type="Stock",
            account_currency=currency,
        )
        cls.company.default_inventory_account = cls.stock_account
        cls.company.save(update_fields=("default_inventory_account",))
        cls.warehouse = Warehouse.objects.create(
            warehouse_name="Stores", company=cls.company
        )
        cls.uom = UnitOfMeasure.objects.create(name="Nos")
        root = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        group = ItemGroup.objects.create(name="Products", parent_item_group=root)
        cls.item = Item.objects.create(
            name="ITEM-001", item_group=group, stock_uom=cls.uom
        )
        cls.fiscal_year = FiscalYear.objects.create(
            year="2026",
            year_start_date=date(2026, 1, 1),
            year_end_date=date(2026, 12, 31),
        )

    def post(self, voucher_no, quantity, rate=None, *, day=1):
        return post_stock_entries(
            company=self.company,
            posting_date=date(2026, 1, day),
            posting_time=time(9),
            voucher_type="Stock Entry",
            voucher_no=voucher_no,
            lines=(
                StockLedgerLine(
                    item=self.item,
                    warehouse=self.warehouse,
                    quantity=Decimal(str(quantity)),
                    incoming_rate=None if rate is None else Decimal(str(rate)),
                    voucher_detail_no=f"{voucher_no}-1",
                ),
            ),
        )

    def test_fifo_posts_immutable_ledger_and_updates_bin(self):
        first = self.post("MAT-001", 10, 5)[0]
        self.post("MAT-002", 5, 8)
        outgoing = self.post("MAT-003", -12)[0]

        item_bin = Bin.objects.get(item=self.item, warehouse=self.warehouse)
        self.assertEqual(item_bin.actual_qty, Decimal("3"))
        self.assertEqual(item_bin.projected_qty, Decimal("3"))
        self.assertEqual(item_bin.stock_value, Decimal("24"))
        self.assertEqual(item_bin.valuation_rate, Decimal("8"))
        self.assertEqual(outgoing.outgoing_rate, Decimal("5.5"))
        self.assertEqual(outgoing.stock_value_difference, Decimal("-66"))
        self.assertEqual(outgoing.stock_queue, [["3.000000000", "8.000000000"]])
        self.assertEqual(first.fiscal_year, self.fiscal_year)

        first.actual_qty = Decimal("99")
        with self.assertRaises(ValidationError):
            first.save()
        with self.assertRaises(ValidationError):
            StockLedgerEntry.objects.filter(pk=first.pk).update(actual_qty=99)
        item_bin.actual_qty = 99
        with self.assertRaises(ValidationError):
            item_bin.save()

    def test_lifo_consumes_newest_layer_first(self):
        self.company.valuation_method = Company.ValuationMethod.LIFO
        self.company.save(update_fields=("valuation_method",))
        self.post("LIFO-001", 10, 5)
        self.post("LIFO-002", 5, 8)
        outgoing = self.post("LIFO-003", -12)[0]

        item_bin = Bin.objects.get(item=self.item, warehouse=self.warehouse)
        self.assertEqual(outgoing.outgoing_rate, Decimal("6.25"))
        self.assertEqual(item_bin.actual_qty, Decimal("3"))
        self.assertEqual(item_bin.stock_value, Decimal("15"))
        self.assertEqual(outgoing.stock_queue, [["3.000000000", "5.000000000"]])

    def test_moving_average_keeps_rate_on_outgoing_stock(self):
        self.company.valuation_method = Company.ValuationMethod.MOVING_AVERAGE
        self.company.save(update_fields=("valuation_method",))
        self.post("MA-001", 10, 5)
        self.post("MA-002", 10, 7)
        outgoing = self.post("MA-003", -5)[0]

        item_bin = Bin.objects.get(item=self.item, warehouse=self.warehouse)
        self.assertEqual(outgoing.outgoing_rate, Decimal("6"))
        self.assertEqual(item_bin.actual_qty, Decimal("15"))
        self.assertEqual(item_bin.stock_value, Decimal("90"))
        self.assertEqual(item_bin.valuation_rate, Decimal("6"))

    def test_duplicate_backdated_and_negative_postings_are_rejected(self):
        self.post("GUARD-001", 3, 5, day=2)
        with self.assertRaises(ValidationError):
            self.post("GUARD-001", 1, 5, day=3)
        with self.assertRaises(ValidationError):
            self.post("GUARD-002", 1, 5, day=1)
        with self.assertRaises(ValidationError):
            self.post("GUARD-003", -4, day=3)

        item_bin = Bin.objects.get(item=self.item, warehouse=self.warehouse)
        self.assertEqual(item_bin.actual_qty, Decimal("3"))
        self.assertEqual(StockLedgerEntry.objects.count(), 1)

    def test_multi_line_failure_rolls_back_every_line(self):
        with self.assertRaises(ValidationError):
            post_stock_entries(
                company=self.company,
                posting_date=date(2026, 1, 1),
                posting_time=time(9),
                voucher_type="Stock Entry",
                voucher_no="ROLLBACK-001",
                lines=(
                    StockLedgerLine(self.item, self.warehouse, Decimal("2"), Decimal("5")),
                    StockLedgerLine(self.item, self.warehouse, Decimal("-3")),
                ),
            )
        self.assertFalse(Bin.objects.exists())
        self.assertFalse(StockLedgerEntry.objects.exists())

    def test_ledger_activity_protects_warehouse_and_item_structure(self):
        self.warehouse.account = self.stock_account
        self.warehouse.save()
        self.post("LOCK-001", 1, 5)
        self.warehouse.is_group = True
        with self.assertRaises(ValidationError):
            self.warehouse.save()
        self.warehouse.refresh_from_db()
        self.warehouse.disabled = True
        with self.assertRaises(ValidationError):
            self.warehouse.save()
        self.warehouse.refresh_from_db()
        self.warehouse.account = None
        with self.assertRaises(ValidationError):
            self.warehouse.save()
        with self.assertRaises(ValidationError):
            self.warehouse.delete()

        alternate_uom = UnitOfMeasure.objects.create(name="Box")
        self.item.stock_uom = alternate_uom
        with self.assertRaises(ValidationError):
            self.item.save()
        self.item.refresh_from_db()
        self.item.is_stock_item = False
        with self.assertRaises(ValidationError):
            self.item.save()
        self.company.valuation_method = Company.ValuationMethod.LIFO
        with self.assertRaises(ValidationError):
            self.company.save()

    def test_posting_validation_rejects_invalid_lines(self):
        with self.assertRaises(ValidationError):
            self.post("ZERO-001", 0, 5)
        with self.assertRaises(ValidationError):
            self.post("RATE-001", 1)
        with self.assertRaises(ValidationError):
            self.post("RATE-002", -1, 5)

        self.item.disabled = True
        self.item.save()
        with self.assertRaises(ValidationError):
            self.post("DISABLED-001", 1, 5)

    def test_whole_number_stock_uom_rejects_fractional_quantity(self):
        self.uom.must_be_whole_number = True
        self.uom.save(update_fields=("must_be_whole_number",))
        with self.assertRaises(ValidationError):
            self.post("FRACTION-001", Decimal("1.5"), 5)
