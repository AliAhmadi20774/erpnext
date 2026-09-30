import csv
import io
from datetime import date, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounting.models import FiscalYear
from catalog.models import Item, ItemGroup, UnitOfMeasure
from geo.models import Country, Currency
from organizations.models import Company

from .entries import submit_stock_entry
from .ledger import StockLedgerLine, post_stock_entries
from .models import StockEntry, StockEntryDetail, StockEntryType, Warehouse
from .repost import cancel_stock_entry
from .warehouse_balance_report import warehouse_balance_report


class WarehouseBalanceReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Warehouse Report Company", abbr="WRC", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.root = Warehouse.objects.create(
            warehouse_name="All Warehouses", company=cls.company, is_group=True,
        )
        cls.subgroup = Warehouse.objects.create(
            warehouse_name="Raw Materials", company=cls.company, is_group=True,
            parent_warehouse=cls.root,
        )
        cls.stores = Warehouse.objects.create(
            warehouse_name="Stores", company=cls.company, parent_warehouse=cls.subgroup,
        )
        cls.finished = Warehouse.objects.create(
            warehouse_name="Finished", company=cls.company, parent_warehouse=cls.root,
        )
        cls.empty_root = Warehouse.objects.create(
            warehouse_name="Spare", company=cls.company,
        )
        cls.uom = UnitOfMeasure.objects.create(name="Nos")
        group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        cls.item = Item.objects.create(name="WH-ITEM", item_group=group, stock_uom=cls.uom)
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

    def entry(self, name, day, qty, *, warehouse, rate=None):
        receipt = rate is not None
        entry = StockEntry.objects.create(
            name=name, company=self.company,
            stock_entry_type=self.receipt_type if receipt else self.issue_type,
            posting_date=date(2026, 1, day), posting_time=time(9),
            to_warehouse=warehouse if receipt else None,
            from_warehouse=None if receipt else warehouse,
        )
        StockEntryDetail.objects.create(
            stock_entry=entry, position=1, item=self.item,
            target_warehouse=warehouse if receipt else None,
            source_warehouse=None if receipt else warehouse,
            qty=Decimal(qty), uom=self.uom, conversion_factor=Decimal("1"),
            basic_rate=Decimal(rate) if receipt else Decimal("0"),
        )
        return submit_stock_entry(entry)

    def balances(self, **filters):
        report = warehouse_balance_report(company=self.company, **filters)
        return {row.warehouse.pk: (row.stock_balance, row.indent) for row in report.rows}

    def test_nested_rollup_and_cancelled_ledger_rows(self):
        self.entry("RECEIPT-1", 1, "10", warehouse=self.stores, rate="5")
        finished_receipt = self.entry("RECEIPT-2", 1, "3", warehouse=self.finished, rate="7")
        self.entry("ISSUE-3", 2, "4", warehouse=self.stores)
        self.assertEqual(self.balances(), {
            self.root.pk: (Decimal("51"), 0),
            self.subgroup.pk: (Decimal("30"), 1),
            self.stores.pk: (Decimal("30"), 2),
            self.finished.pk: (Decimal("21"), 1),
            self.empty_root.pk: (Decimal("0"), 0),
        })
        cancel_stock_entry(finished_receipt)
        balances = self.balances()
        self.assertEqual((balances[self.root.pk][0], balances[self.finished.pk][0]),
                         (Decimal("30"), Decimal("0")))

    def test_disabled_warehouse_visibility_and_company_boundary(self):
        self.entry("RECEIPT-1", 1, "2", warehouse=self.stores, rate="5")
        self.entry("ISSUE-2", 2, "2", warehouse=self.stores)
        self.stores.disabled = True
        self.stores.save()
        self.assertNotIn(self.stores.pk, self.balances())
        self.assertEqual(self.balances()[self.root.pk][0], Decimal("0"))
        self.assertIn(self.stores.pk, self.balances(show_disabled_warehouses=True))
        foreign = Company.objects.create(
            name="Foreign", abbr="FC", country=self.company.country,
            default_currency=self.company.default_currency, enable_perpetual_inventory=False,
        )
        Warehouse.objects.create(warehouse_name="Foreign", company=foreign)
        self.assertEqual(len(self.balances(show_disabled_warehouses=True)), 5)

    def test_page_csv_and_permissions(self):
        self.entry("RECEIPT-1", 1, "2", warehouse=self.stores, rate="5")
        url = reverse("warehouse_balance_report")
        params = {"company": self.company.pk}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        viewer.user_permissions.add(Permission.objects.get(codename="view_stockledgerentry"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        self.assertContains(self.client.get(url, params), self.root.pk)
        response = self.client.get(url, params | {"format": "csv"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(rows[0], ["Warehouse", "Parent Warehouse", "Depth",
                                   "Is Group", "Disabled", "Stock Balance"])
        self.assertEqual(rows[1][0], self.root.pk)
        self.assertEqual(rows[1][5], "10")

    def test_historical_value_and_single_item_quantity(self):
        self.entry("RECEIPT-1", 1, "10", warehouse=self.stores, rate="5")
        other_uom = UnitOfMeasure.objects.create(name="Kg")
        other_item = Item.objects.create(
            name="WH-WEIGHT", item_group=self.item.item_group, stock_uom=other_uom,
        )
        post_stock_entries(
            company=self.company, posting_date=date(2026, 1, 1), posting_time=time(10),
            voucher_type="Stock Entry", voucher_no="RECEIPT-WEIGHT",
            lines=[StockLedgerLine(
                item=other_item, warehouse=self.stores, quantity=Decimal("3"),
                incoming_rate=Decimal("2"),
            )],
        )
        self.entry("ISSUE-2", 2, "4", warehouse=self.stores)
        self.entry("RECEIPT-3", 3, "2", warehouse=self.stores, rate="7")

        historical = warehouse_balance_report(
            company=self.company, as_on_date=date(2026, 1, 2),
        )
        root = next(row for row in historical.rows if row.warehouse == self.root)
        self.assertEqual(root.stock_balance, Decimal("36"))
        self.assertIsNone(root.stock_qty)
        self.assertIsNone(historical.stock_uom)
        self.assertEqual(self.balances()[self.root.pk][0], Decimal("50"))

        item_report = warehouse_balance_report(
            company=self.company, item=self.item, as_on_date=date(2026, 1, 2),
        )
        root = next(row for row in item_report.rows if row.warehouse == self.root)
        subgroup = next(row for row in item_report.rows if row.warehouse == self.subgroup)
        self.assertEqual((root.stock_balance, root.stock_qty, subgroup.stock_qty),
                         (Decimal("30"), Decimal("6"), Decimal("6")))
        self.assertEqual(item_report.stock_uom, "Nos")
        self.assertEqual(warehouse_balance_report(
            company=self.company, item=self.item, as_on_date=date(2025, 12, 31),
        ).rows[0].stock_qty, Decimal("0"))
        with self.assertRaises(ValidationError):
            warehouse_balance_report(company=self.company, as_on_date="2026-01-02")
        with self.assertRaises(TypeError):
            warehouse_balance_report(company=self.company, item="WH-ITEM")

        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        url = reverse("warehouse_balance_report")
        params = {"company": self.company.pk, "item": self.item.pk,
                  "as_on_date": "2026-01-02"}
        self.assertContains(self.client.get(url, params), "Qty (Nos)")
        response = self.client.get(url, params | {"format": "csv"})
        self.assertIn("2026-01-02", response["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(rows[0][-2:], ["Stock Balance", "Qty (Nos)"])
        self.assertEqual(rows[1][-2:], ["30", "6"])
