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
from projects.models import Project

from .entries import submit_stock_entry
from .models import (
    StockEntry, StockEntryDetail, StockEntryType, StockReconciliation,
    StockReconciliationItem, Warehouse,
)
from .reconciliation import cancel_stock_reconciliation, submit_stock_reconciliation
from .stock_ledger_report import stock_ledger_report


class StockLedgerReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Report Company", abbr="RC", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.root_warehouse = Warehouse.objects.create(
            warehouse_name="All Warehouses", company=cls.company, is_group=True,
        )
        cls.stores = Warehouse.objects.create(
            warehouse_name="Stores", company=cls.company,
            parent_warehouse=cls.root_warehouse,
        )
        cls.uom = UnitOfMeasure.objects.create(name="Nos")
        root_group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        cls.item = Item.objects.create(
            name="REPORT-ITEM", item_group=root_group, stock_uom=cls.uom,
        )
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

    def stock_entry(self, *, name, day, qty, rate=None):
        receipt = rate is not None
        entry = StockEntry.objects.create(
            name=name, company=self.company,
            stock_entry_type=self.receipt_type if receipt else self.issue_type,
            posting_date=date(2026, 1, day), posting_time=time(9),
            to_warehouse=self.stores if receipt else None,
            from_warehouse=None if receipt else self.stores,
        )
        StockEntryDetail.objects.create(
            stock_entry=entry, position=1, item=self.item,
            target_warehouse=self.stores if receipt else None,
            source_warehouse=None if receipt else self.stores,
            qty=Decimal(qty), uom=self.uom, conversion_factor=Decimal("1"),
            basic_rate=Decimal(rate) if receipt else Decimal("0"),
        )
        return submit_stock_entry(entry)

    def test_opening_group_warehouse_and_direct_value_change(self):
        self.stock_entry(name="RECEIPT-1", day=1, qty="10", rate="5")
        self.stock_entry(name="ISSUE-2", day=2, qty="4")
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("6"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        report = stock_ledger_report(
            company=self.company, from_date=date(2026, 1, 2), to_date=date(2026, 1, 3),
            item=self.item, warehouse=self.root_warehouse,
        )
        self.assertEqual((report.opening.quantity, report.opening.stock_value,
                          report.opening.valuation_rate),
                         (Decimal("10"), Decimal("50"), Decimal("5")))
        self.assertEqual([(row.entry.voucher_no, row.in_qty, row.out_qty,
                           row.entry.qty_after_transaction, row.entry.stock_value_difference)
                          for row in report.rows],
                         [("ISSUE-2", Decimal("0"), Decimal("-4"), Decimal("6"), Decimal("-20")),
                          (reconciliation.pk, Decimal("0"), Decimal("0"), Decimal("6"), Decimal("18"))])
        cancel_stock_reconciliation(reconciliation)
        report = stock_ledger_report(
            company=self.company, from_date=date(2026, 1, 3), to_date=date(2026, 1, 3),
            item=self.item, warehouse=self.stores,
        )
        self.assertEqual(report.rows, ())
        self.assertEqual(report.opening.stock_value, Decimal("30"))

    def test_filters_and_company_validation(self):
        self.stock_entry(name="RECEIPT-1", day=1, qty="10", rate="5")
        project = Project.objects.create(
            name="PRJ-1", project_name="Project 1", company=self.company,
        )
        report = stock_ledger_report(
            company=self.company, from_date=date(2026, 1, 1), to_date=date(2026, 1, 2),
            item=self.item, warehouse=self.stores, voucher_no="RECEIPT-1",
        )
        self.assertEqual(len(report.rows), 1)
        self.assertIsNone(report.opening)
        self.assertEqual(stock_ledger_report(
            company=self.company, from_date=date(2026, 1, 1), to_date=date(2026, 1, 2),
            project=project,
        ).rows, ())
        foreign = Company.objects.create(
            name="Other Company", abbr="OC", country=self.company.country,
            default_currency=self.company.default_currency,
            enable_perpetual_inventory=False,
        )
        foreign_warehouse = Warehouse.objects.create(
            warehouse_name="Foreign", company=foreign,
        )
        with self.assertRaises(ValidationError):
            stock_ledger_report(company=self.company, from_date=date(2026, 1, 1),
                                to_date=date(2026, 1, 2), warehouse=foreign_warehouse)
        with self.assertRaises(ValidationError):
            stock_ledger_report(company=self.company, from_date=date(2026, 1, 2),
                                to_date=date(2026, 1, 1))

    def test_page_csv_and_permission(self):
        self.stock_entry(name="RECEIPT-1", day=1, qty="10", rate="5")
        url = reverse("stock_ledger_report")
        params = {"company": self.company.pk, "from_date": "2026-01-01",
                  "to_date": "2026-01-02", "item": self.item.pk,
                  "warehouse": self.stores.pk}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        self.assertContains(self.client.get(url, params), "RECEIPT-1")
        response = self.client.get(url, params | {"format": "csv"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(rows[0][0:6], ["Date", "Item", "Stock UOM", "In Qty",
                                         "Out Qty", "Balance Qty"])
        self.assertEqual(rows[1][0], "Opening")
        self.assertEqual((rows[2][1], rows[2][5], rows[2][13]),
                         (self.item.pk, "10.000000000", "RECEIPT-1"))
