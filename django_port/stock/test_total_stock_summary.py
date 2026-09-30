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
from .models import Warehouse
from .total_stock_summary import total_stock_summary


class TotalStockSummaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        other_currency = Currency.objects.create(name="USD")
        cls.company = Company.objects.create(
            name="Summary Company", abbr="SC", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.other_company = Company.objects.create(
            name="Other Company", abbr="OC", country=country,
            default_currency=other_currency, enable_perpetual_inventory=False,
        )
        cls.stores = Warehouse.objects.create(warehouse_name="Stores", company=cls.company)
        cls.finished = Warehouse.objects.create(warehouse_name="Finished", company=cls.company)
        cls.other_stores = Warehouse.objects.create(
            warehouse_name="Stores", company=cls.other_company,
        )
        uom = UnitOfMeasure.objects.create(name="Nos")
        group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        cls.item = Item.objects.create(
            name="SUM-ITEM", item_group=group, stock_uom=uom,
            description="Summary item",
        )
        cls.empty_item = Item.objects.create(name="ZERO-ITEM", item_group=group, stock_uom=uom)
        FiscalYear.objects.create(
            year="2026", year_start_date=date(2026, 1, 1),
            year_end_date=date(2026, 12, 31),
        )

    def post(self, name, *, company, warehouse, item, qty, rate=None, day=1):
        post_stock_entries(
            company=company, posting_date=date(2026, 1, day), posting_time=time(9),
            voucher_type="Stock Entry", voucher_no=name,
            lines=[StockLedgerLine(
                item=item, warehouse=warehouse, quantity=Decimal(qty),
                incoming_rate=Decimal(rate) if rate is not None else None,
            )],
        )

    def seed_balances(self):
        self.post("RECEIPT-1", company=self.company, warehouse=self.stores,
                  item=self.item, qty="10", rate="5")
        self.post("RECEIPT-2", company=self.company, warehouse=self.finished,
                  item=self.item, qty="5", rate="6")
        self.post("RECEIPT-3", company=self.other_company, warehouse=self.other_stores,
                  item=self.item, qty="4", rate="7")
        self.post("RECEIPT-4", company=self.company, warehouse=self.stores,
                  item=self.empty_item, qty="2", rate="2")
        self.post("ISSUE-5", company=self.company, warehouse=self.stores,
                  item=self.empty_item, qty="-2")

    def test_warehouse_and_company_grouping_uses_current_nonzero_bins(self):
        self.seed_balances()
        warehouse_rows = total_stock_summary(group_by="Warehouse", company=self.company).rows
        self.assertEqual([(row.group_name, row.item_code, row.description, row.current_qty)
                          for row in warehouse_rows],
                         [(self.finished.pk, "SUM-ITEM", "Summary item", Decimal("5")),
                          (self.stores.pk, "SUM-ITEM", "Summary item", Decimal("10"))])
        company_rows = total_stock_summary(group_by="Company").rows
        self.assertEqual([(row.group_name, row.item_code, row.current_qty)
                          for row in company_rows],
                         [(self.other_company.pk, "SUM-ITEM", Decimal("4")),
                          (self.company.pk, "SUM-ITEM", Decimal("15"))])
        self.assertEqual([row.current_qty for row in total_stock_summary(
            group_by="Company", company=self.company,
        ).rows], [Decimal("15")])
        self.assertEqual([(row.group_name, row.stock_value, row.currency)
                          for row in company_rows],
                         [(self.other_company.pk, Decimal("28"), "USD"),
                          (self.company.pk, Decimal("80"), "IRR")])

    def test_historical_quantity_value_and_company_currency(self):
        self.seed_balances()
        self.post("RECEIPT-6", company=self.company, warehouse=self.stores,
                  item=self.item, qty="2", rate="8", day=2)

        historical = total_stock_summary(
            group_by="Warehouse", company=self.company, as_on_date=date(2026, 1, 1),
        )
        self.assertEqual([(row.group_name, row.item_code, row.current_qty,
                           row.stock_value, row.currency) for row in historical.rows],
                         [(self.finished.pk, "SUM-ITEM", Decimal("5"), Decimal("30"), "IRR"),
                          (self.stores.pk, "SUM-ITEM", Decimal("10"), Decimal("50"), "IRR")])
        self.assertEqual(historical.as_on_date, date(2026, 1, 1))
        current = total_stock_summary(group_by="Company", company=self.company).rows[0]
        self.assertEqual((current.current_qty, current.stock_value),
                         (Decimal("17"), Decimal("96")))
        historical_companies = total_stock_summary(
            group_by="Company", as_on_date=date(2026, 1, 1),
        ).rows
        self.assertEqual([(row.group_name, row.currency) for row in historical_companies],
                         [(self.other_company.pk, "USD"), (self.company.pk, "IRR")])
        with self.assertRaises(ValidationError):
            total_stock_summary(group_by="Company", as_on_date="2026-01-01")

        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        url = reverse("total_stock_summary_report")
        params = {"group_by": "Warehouse", "company": self.company.pk,
                  "as_on_date": "2026-01-01"}
        self.assertContains(self.client.get(url, params), "Stock Value")
        response = self.client.get(url, params | {"format": "csv"})
        self.assertIn("2026-01-01", response["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(rows[0],
                         ["Warehouse", "Item", "Description", "Qty", "Stock Value", "Currency"])
        self.assertEqual(rows[1][-3:], ["5", "30", "IRR"])

    def test_group_validation_and_page_csv_permission(self):
        self.seed_balances()
        with self.assertRaises(ValidationError):
            total_stock_summary(group_by="Warehouse")
        with self.assertRaises(ValidationError):
            total_stock_summary(group_by="Unknown", company=self.company)
        url = reverse("total_stock_summary_report")
        params = {"group_by": "Warehouse", "company": self.company.pk}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        viewer.user_permissions.add(Permission.objects.get(codename="view_bin"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 200)
        self.assertEqual(self.client.get(
            url, params | {"as_on_date": "2026-01-01"},
        ).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        self.assertContains(self.client.get(url, params), "SUM-ITEM")
        response = self.client.get(url, params | {"format": "csv"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(rows[0], ["Warehouse", "Item", "Description", "Current Qty",
                                   "Stock Value", "Currency"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][1:3], ["SUM-ITEM", "Summary item"])
        self.assertEqual(rows[1][-2:], ["30", "IRR"])
