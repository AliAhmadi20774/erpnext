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

from .ledger import StockLedgerLine, post_stock_entries
from .models import Bin, Warehouse
from .stock_invariant_report import stock_invariant_report


class StockInvariantReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Invariant Company", abbr="IC", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.warehouse = Warehouse.objects.create(warehouse_name="Stores", company=cls.company)
        cls.uom = UnitOfMeasure.objects.create(name="Nos")
        group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        cls.item = Item.objects.create(name="INV-ITEM", item_group=group, stock_uom=cls.uom)
        FiscalYear.objects.create(
            year="2026", year_start_date=date(2026, 1, 1),
            year_end_date=date(2026, 12, 31),
        )

    def post(self, name, day, qty, rate=None):
        return post_stock_entries(
            company=self.company, posting_date=date(2026, 1, day),
            posting_time=time(9), voucher_type="Stock Entry", voucher_no=name,
            lines=[StockLedgerLine(
                item=self.item, warehouse=self.warehouse, quantity=Decimal(qty),
                incoming_rate=Decimal(rate) if rate is not None else None,
            )],
        )[0]

    def report(self, **filters):
        return stock_invariant_report(
            company=self.company, item=self.item, warehouse=self.warehouse, **filters,
        )

    def test_fifo_ledger_queue_and_bin_then_snapshot_difference(self):
        self.post("RECEIPT-1", 1, "10", "5")
        self.post("RECEIPT-2", 2, "2", "7")
        issue = self.post("ISSUE-3", 3, "-4")
        report = self.report()
        self.assertFalse(report.has_issues)
        self.assertEqual([(row.expected_qty, row.expected_value, row.has_issue)
                          for row in report.rows],
                         [(Decimal("10"), Decimal("50"), False),
                          (Decimal("12"), Decimal("64"), False),
                          (Decimal("8"), Decimal("44"), False)])
        self.assertEqual((report.bin_check.expected_qty, report.bin_check.expected_value),
                         (Decimal("8"), Decimal("44")))

        issue.stock_value = Decimal("45")
        issue.save(_allow_repost=True, update_fields=["stock_value"])
        report = self.report(show_incorrect_entries=True)
        self.assertTrue(report.has_issues)
        self.assertEqual(len(report.rows), 2)
        self.assertEqual((report.rows[-1].value_difference,
                          report.rows[-1].queue_value_difference),
                         (Decimal("1"), Decimal("1")))
        self.assertFalse(report.bin_check.has_issue)

    def test_invalid_queue_bin_difference_and_company_validation(self):
        entry = self.post("RECEIPT-1", 1, "3", "5")
        entry.stock_queue = [["invalid", "5"]]
        entry.save(_allow_repost=True, update_fields=["stock_queue"])
        item_bin = Bin.objects.get(item=self.item, warehouse=self.warehouse)
        item_bin.actual_qty = Decimal("4")
        item_bin.save(_allow_stock_write=True)
        report = self.report()
        self.assertTrue(report.rows[0].queue_error)
        self.assertTrue(report.bin_check.has_issue)
        self.assertEqual(report.bin_check.qty_difference, Decimal("1"))
        foreign = Company.objects.create(
            name="Foreign", abbr="FC", country=self.company.country,
            default_currency=self.company.default_currency, enable_perpetual_inventory=False,
        )
        foreign_warehouse = Warehouse.objects.create(warehouse_name="Foreign", company=foreign)
        with self.assertRaises(ValidationError):
            stock_invariant_report(company=self.company, item=self.item,
                                   warehouse=foreign_warehouse)

    def test_zero_quantity_value_reset_keeps_invariants(self):
        self.post("RECEIPT-1", 1, "3", "5")
        post_stock_entries(
            company=self.company, posting_date=date(2026, 1, 2),
            posting_time=time(9), voucher_type="Stock Reconciliation", voucher_no="RESET-2",
            lines=[StockLedgerLine(
                item=self.item, warehouse=self.warehouse, quantity=Decimal("0"),
                incoming_rate=Decimal("8"), is_value_adjustment=True,
            )],
        )
        report = self.report()
        self.assertFalse(report.has_issues)
        self.assertEqual((report.rows[-1].expected_qty, report.rows[-1].expected_value),
                         (Decimal("3"), Decimal("24")))

    def test_moving_average_page_csv_and_permissions(self):
        self.company.valuation_method = Company.ValuationMethod.MOVING_AVERAGE
        self.company.save()
        self.post("RECEIPT-1", 1, "3", "5")
        self.assertFalse(self.report().has_issues)
        self.assertIsNone(self.report().rows[0].queue_qty_difference)

        url = reverse("stock_invariant_report")
        params = {"company": self.company.pk, "item": self.item.pk,
                  "warehouse": self.warehouse.pk}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        viewer.user_permissions.add(Permission.objects.get(codename="view_stockledgerentry"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        self.assertContains(self.client.get(url, params), "No differences found")
        response = self.client.get(url, params | {"format": "csv"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual([row[0] for row in rows[1:]],
                         [self.report().rows[0].entry.pk, "Bin"])
