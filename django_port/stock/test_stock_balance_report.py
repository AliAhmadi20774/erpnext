import csv
import io
from datetime import date, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounting.models import FiscalYear
from catalog.models import Item, ItemGroup, UnitOfMeasure
from geo.models import Country, Currency
from organizations.models import Company

from .entries import submit_stock_entry
from .models import (StockEntry, StockEntryDetail, StockEntryType,
                     StockReconciliation, StockReconciliationItem, Warehouse, WarehouseType)
from .reconciliation import cancel_stock_reconciliation, submit_stock_reconciliation
from .stock_balance_report import stock_balance_report


class StockBalanceReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Balance Company", abbr="BC", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.main_type = WarehouseType.objects.create(name="Main")
        cls.other_type = WarehouseType.objects.create(name="Other")
        cls.root_warehouse = Warehouse.objects.create(
            warehouse_name="All Warehouses", company=cls.company, is_group=True,
        )
        cls.stores = Warehouse.objects.create(
            warehouse_name="Stores", company=cls.company,
            parent_warehouse=cls.root_warehouse, warehouse_type=cls.main_type,
        )
        cls.secondary = Warehouse.objects.create(
            warehouse_name="Secondary", company=cls.company,
            parent_warehouse=cls.root_warehouse, warehouse_type=cls.other_type,
        )
        cls.uom = UnitOfMeasure.objects.create(name="Nos")
        root_group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        cls.group = ItemGroup.objects.create(
            name="Report Group", parent_item_group=root_group, is_group=True,
        )
        leaf = ItemGroup.objects.create(name="Report Leaf", parent_item_group=cls.group)
        cls.item = Item.objects.create(name="BAL-ITEM", item_group=leaf, stock_uom=cls.uom)
        FiscalYear.objects.create(
            year="2026", year_start_date=date(2026, 1, 1),
            year_end_date=date(2026, 12, 31),
        )
        cls.receipt_type = StockEntryType.objects.create(
            name="Material Receipt", purpose=StockEntryType.Purpose.MATERIAL_RECEIPT,
        )
        cls.issue_type = StockEntryType.objects.create(
            name="Material Issue", purpose=StockEntryType.Purpose.MATERIAL_ISSUE,
        )

    def stock_entry(self, name, day, qty, *, warehouse=None, item=None, rate=None):
        warehouse = warehouse or self.stores
        item = item or self.item
        receipt = rate is not None
        entry = StockEntry.objects.create(
            name=name, company=self.company,
            stock_entry_type=self.receipt_type if receipt else self.issue_type,
            posting_date=date(2026, 1, day), posting_time=time(9),
            to_warehouse=warehouse if receipt else None,
            from_warehouse=None if receipt else warehouse,
        )
        StockEntryDetail.objects.create(
            stock_entry=entry, position=1, item=item,
            target_warehouse=warehouse if receipt else None,
            source_warehouse=None if receipt else warehouse,
            qty=Decimal(qty), uom=self.uom, conversion_factor=Decimal("1"),
            basic_rate=Decimal(rate) if receipt else Decimal("0"),
        )
        return submit_stock_entry(entry)

    def report(self, start=2, end=3, **filters):
        return stock_balance_report(
            company=self.company, from_date=date(2026, 1, start),
            to_date=date(2026, 1, end), **filters,
        )

    def test_period_balances_value_reset_and_cancellation(self):
        self.stock_entry("RECEIPT-1", 1, "10", rate="5")
        self.stock_entry("ISSUE-2", 2, "4")
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("6"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        row = self.report(item=self.item, warehouse=self.root_warehouse).rows[0]
        self.assertEqual(
            (row.opening_qty, row.opening_value, row.in_qty, row.in_value,
             row.out_qty, row.out_value, row.balance_qty, row.balance_value,
             row.valuation_rate),
            (Decimal("10"), Decimal("50"), Decimal("0"), Decimal("18"),
             Decimal("4"), Decimal("20"), Decimal("6"), Decimal("48"), Decimal("8")),
        )
        earlier = self.report(end=2).rows[0]
        self.assertEqual((earlier.balance_qty, earlier.balance_value),
                         (Decimal("6"), Decimal("30")))
        cancel_stock_reconciliation(reconciliation)
        row = self.report(item=self.item).rows[0]
        self.assertEqual((row.in_value, row.balance_value, row.valuation_rate),
                         (Decimal("0"), Decimal("30"), Decimal("5")))

    def test_group_type_zero_rows_and_company_boundary(self):
        self.stock_entry("RECEIPT-1", 1, "3", rate="5")
        self.stock_entry("ISSUE-2", 2, "3")
        self.stock_entry("RECEIPT-3", 1, "2", warehouse=self.secondary, rate="7")
        other_group = ItemGroup.objects.create(name="Other Group", parent_item_group=self.group.parent_item_group)
        other_item = Item.objects.create(name="OTHER-ITEM", item_group=other_group, stock_uom=self.uom)
        self.stock_entry("RECEIPT-4", 1, "1", item=other_item, rate="11")
        self.assertEqual(self.report(item_group=self.group, warehouse_type=self.main_type).rows, ())
        zero_row = self.report(item_group=self.group, warehouse_type=self.main_type,
                               include_zero_stock=True).rows[0]
        self.assertEqual((zero_row.opening_qty, zero_row.out_qty, zero_row.balance_qty),
                         (Decimal("3"), Decimal("3"), Decimal("0")))
        rows = self.report(item_group=self.group, warehouse=self.root_warehouse).rows
        self.assertEqual([(row.warehouse.pk, row.balance_value) for row in rows],
                         [(self.secondary.pk, Decimal("14"))])
        foreign = Company.objects.create(
            name="Foreign", abbr="FG", country=self.company.country,
            default_currency=self.company.default_currency, enable_perpetual_inventory=False,
        )
        foreign_warehouse = Warehouse.objects.create(warehouse_name="Foreign", company=foreign)
        with self.assertRaises(ValidationError):
            self.report(warehouse=foreign_warehouse)
        with self.assertRaises(ValidationError):
            self.report(start=3, end=2)

    def test_page_csv_and_permissions(self):
        self.stock_entry("RECEIPT-1", 1, "10", rate="5")
        url = reverse("stock_balance_report")
        params = {"company": self.company.pk, "from_date": "2026-01-01",
                  "to_date": "2026-01-02", "item": self.item.pk,
                  "warehouse": self.root_warehouse.pk}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        self.assertContains(self.client.get(url, params), "BAL-ITEM")
        response = self.client.get(url, params | {"format": "csv"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(rows[0][:4], ["Item", "Item Name", "Item Group", "Warehouse"])
        self.assertEqual((rows[1][0], rows[1][11], rows[1][12]),
                         ("BAL-ITEM", "10.000000000", "50.000000000"))
