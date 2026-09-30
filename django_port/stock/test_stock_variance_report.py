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
from .stock_variance_report import stock_variance_report


class StockVarianceReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Variance Company", abbr="VC", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.warehouse = Warehouse.objects.create(warehouse_name="Stores", company=cls.company)
        cls.uom = UnitOfMeasure.objects.create(name="Nos")
        group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        cls.first_item = Item.objects.create(name="VAR-1", item_group=group, stock_uom=cls.uom)
        cls.second_item = Item.objects.create(name="VAR-2", item_group=group, stock_uom=cls.uom)
        FiscalYear.objects.create(
            year="2026", year_start_date=date(2026, 1, 1),
            year_end_date=date(2026, 12, 31),
        )

    def post(self, name, day, qty, *, item=None, rate=None):
        return post_stock_entries(
            company=self.company, posting_date=date(2026, 1, day),
            posting_time=time(9), voucher_type="Stock Entry", voucher_no=name,
            lines=[StockLedgerLine(
                item=item or self.first_item, warehouse=self.warehouse,
                quantity=Decimal(qty),
                incoming_rate=Decimal(rate) if rate is not None else None,
            )],
        )[0]

    def report(self, **filters):
        return stock_variance_report(company=self.company, **filters)

    def test_first_difference_per_pair_filters_and_bin_difference(self):
        self.post("RECEIPT-1", 1, "10", rate="5")
        issue = self.post("ISSUE-2", 2, "-4")
        self.post("RECEIPT-3", 1, "3", item=self.second_item, rate="7")
        self.assertEqual(self.report().rows, ())

        issue.qty_after_transaction = Decimal("7")
        issue.save(_allow_repost=True, update_fields=["qty_after_transaction"])
        item_bin = Bin.objects.get(item=self.second_item, warehouse=self.warehouse)
        item_bin.stock_value = Decimal("22")
        item_bin.save(_allow_stock_write=True)
        rows = self.report().rows
        self.assertEqual([(row.item.pk, row.source) for row in rows],
                         [("VAR-1", "Stock Ledger Entry"), ("VAR-2", "Bin")])
        self.assertEqual(rows[0].qty_difference, Decimal("1"))
        self.assertEqual(rows[1].value_difference, Decimal("1"))
        self.assertEqual([row.item.pk for row in self.report(difference_in="Qty").rows],
                         ["VAR-1"])
        self.assertEqual([row.item.pk for row in self.report(difference_in="Value").rows],
                         ["VAR-2"])
        self.assertEqual([row.item.pk for row in self.report(item=self.second_item).rows],
                         ["VAR-2"])
        self.second_item.disabled = True
        self.second_item.save()
        self.assertEqual([row.item.pk for row in self.report().rows], ["VAR-1"])
        self.assertEqual(len(self.report(include_disabled=True).rows), 2)

    def test_invalid_queue_and_input_validation(self):
        first = self.post("RECEIPT-1", 1, "3", rate="5")
        self.post("RECEIPT-2", 2, "2", rate="7")
        first.stock_queue = [["broken", "5"]]
        first.save(_allow_repost=True, update_fields=["stock_queue"])
        row = self.report(difference_in="Value").rows[0]
        self.assertEqual(row.entry_name, first.pk)
        self.assertTrue(row.queue_error)
        foreign = Company.objects.create(
            name="Foreign", abbr="FC", country=self.company.country,
            default_currency=self.company.default_currency, enable_perpetual_inventory=False,
        )
        foreign_warehouse = Warehouse.objects.create(warehouse_name="Foreign", company=foreign)
        with self.assertRaises(ValidationError):
            self.report(warehouse=foreign_warehouse)
        with self.assertRaises(ValidationError):
            self.report(difference_in="Unknown")

    def test_valuation_filter_includes_queue_rate_difference(self):
        entry = self.post("RECEIPT-1", 1, "3", rate="5")
        entry.stock_queue = [["3", "6"]]
        entry.save(_allow_repost=True, update_fields=["stock_queue"])

        rows = self.report(difference_in="Valuation").rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].entry_name, entry.pk)
        self.assertEqual(rows[0].rate_difference, Decimal("0"))
        self.assertEqual(rows[0].queue_rate_difference, Decimal("-1"))

        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        url = reverse("stock_variance_report")
        params = {"company": self.company.pk, "difference_in": "Valuation"}
        self.assertContains(self.client.get(url, params), "Queue Rate Difference")
        response = self.client.get(url, params | {"format": "csv"})
        csv_rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        index = csv_rows[0].index("Queue Rate Difference")
        self.assertEqual(csv_rows[1][index], "-1.000000000")

    def test_page_csv_and_permission(self):
        entry = self.post("RECEIPT-1", 1, "3", rate="5")
        entry.stock_value = Decimal("16")
        entry.save(_allow_repost=True, update_fields=["stock_value"])
        url = reverse("stock_variance_report")
        params = {"company": self.company.pk, "difference_in": "Value"}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        viewer.user_permissions.add(Permission.objects.get(codename="view_stockledgerentry"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        self.assertContains(self.client.get(url, params), "VAR-1")
        response = self.client.get(url, params | {"format": "csv"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(rows[0][:3], ["Item", "Warehouse", "Valuation Method"])
        self.assertEqual((rows[1][0], rows[1][7]), ("VAR-1", "1.000000000"))
