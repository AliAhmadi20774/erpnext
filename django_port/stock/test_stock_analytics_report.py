import csv
import io
from datetime import date, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounting.models import FiscalYear
from catalog.models import Brand, Item, ItemGroup, UnitOfMeasure
from geo.models import Country, Currency
from organizations.models import Company

from .ledger import StockLedgerLine, post_stock_entries
from .models import Warehouse, WarehouseType
from .stock_analytics_report import stock_analytics_report


class StockAnalyticsReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Analytics Company", abbr="AN", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.main_type = WarehouseType.objects.create(name="Main")
        cls.root = Warehouse.objects.create(warehouse_name="All", company=cls.company,
                                            is_group=True)
        cls.stores = Warehouse.objects.create(warehouse_name="Stores", company=cls.company,
                                               parent_warehouse=cls.root,
                                               warehouse_type=cls.main_type)
        cls.secondary = Warehouse.objects.create(warehouse_name="Secondary", company=cls.company,
                                                  parent_warehouse=cls.root)
        uom = UnitOfMeasure.objects.create(name="Nos")
        root_group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        cls.group = ItemGroup.objects.create(name="Selected Group", parent_item_group=root_group,
                                             is_group=True)
        leaf = ItemGroup.objects.create(name="Leaf", parent_item_group=cls.group)
        cls.brand = Brand.objects.create(name="Analytics Brand")
        cls.item = Item.objects.create(name="AN-ITEM", item_group=leaf, stock_uom=uom,
                                       brand=cls.brand)
        cls.empty_item = Item.objects.create(name="EMPTY-ITEM", item_group=leaf, stock_uom=uom)
        FiscalYear.objects.create(year="2025", year_start_date=date(2025, 1, 1),
                                  year_end_date=date(2025, 12, 31))
        FiscalYear.objects.create(year="2026", year_start_date=date(2026, 1, 1),
                                  year_end_date=date(2026, 12, 31))

    def post(self, name, posted_on, quantity, rate=None, warehouse=None, *, value_reset=False):
        return post_stock_entries(
            company=self.company, posting_date=posted_on, posting_time=time(9),
            voucher_type="Stock Entry", voucher_no=name,
            lines=[StockLedgerLine(item=self.item, warehouse=warehouse or self.stores,
                                   quantity=Decimal(quantity),
                                   incoming_rate=Decimal(rate) if rate is not None else None,
                                   is_value_adjustment=value_reset)],
        )[0]

    def report(self, **filters):
        return stock_analytics_report(company=self.company, **filters)

    def test_monthly_opening_gap_and_value_revaluation(self):
        self.post("RECEIPT-DEC", date(2025, 12, 15), "10", "5")
        self.post("ISSUE-JAN", date(2026, 1, 15), "-5")
        self.post("REVALUE-JAN", date(2026, 1, 16), "0", "8", value_reset=True)
        self.post("RECEIPT-MAR", date(2026, 3, 15), "3", "8")
        filters = dict(from_date=date(2026, 1, 20), to_date=date(2026, 3, 20),
                       item=self.item)
        quantity = self.report(measure="Quantity", **filters)
        self.assertEqual([(p.start, p.end) for p in quantity.periods],
                         [(date(2026, 1, 1), date(2026, 1, 31)),
                          (date(2026, 2, 1), date(2026, 2, 28)),
                          (date(2026, 3, 1), date(2026, 3, 20))])
        self.assertEqual(quantity.rows[0].balances,
                         (Decimal("5"), Decimal("5"), Decimal("8")))
        value = self.report(measure="Value", **filters)
        self.assertEqual(value.rows[0].balances,
                         (Decimal("40"), Decimal("40"), Decimal("64")))
        self.assertEqual(value.chart_data,
                         {"labels": ["Jan 2026", "Feb 2026", "Mar 2026"],
                          "series": [{"item": "AN-ITEM", "balances": ["40.000000000",
                                      "40.000000000", "64.000000000"]}]})

    def test_weekly_quarterly_and_fiscal_year_periods(self):
        self.post("RECEIPT-DEC", date(2025, 12, 30), "3", "5")
        self.post("RECEIPT-JAN", date(2026, 1, 2), "10", "5")
        self.post("ISSUE-JAN", date(2026, 1, 6), "-2")
        self.post("RECEIPT-JAN-2", date(2026, 1, 13), "3", "5")
        self.post("ISSUE-APR", date(2026, 4, 15), "-4")
        weekly = self.report(from_date=date(2026, 1, 1), to_date=date(2026, 1, 14),
                             frequency="Weekly", measure="Quantity", item=self.item)
        self.assertEqual(weekly.rows[0].balances,
                         (Decimal("13"), Decimal("11"), Decimal("14")))
        self.assertEqual(weekly.periods[0].start, date(2025, 12, 29))
        quarterly = self.report(from_date=date(2026, 1, 1), to_date=date(2026, 7, 1),
                                frequency="Quarterly", measure="Quantity", item=self.item)
        self.assertEqual(quarterly.rows[0].balances,
                         (Decimal("14"), Decimal("10"), Decimal("10")))
        yearly = self.report(from_date=date(2025, 12, 30), to_date=date(2026, 1, 31),
                             frequency="Yearly", measure="Quantity", item=self.item)
        self.assertEqual([period.label for period in yearly.periods], ["2025", "2026"])
        self.assertEqual(yearly.rows[0].balances, (Decimal("3"), Decimal("14")))

    def test_item_brand_group_warehouse_and_empty_item(self):
        self.post("RECEIPT-MAIN", date(2026, 1, 2), "4", "5")
        self.post("RECEIPT-SECONDARY", date(2026, 1, 3), "2", "7", self.secondary)
        filters = dict(from_date=date(2026, 1, 1), to_date=date(2026, 1, 31),
                       measure="Quantity", item_group=self.group)
        rows = self.report(**filters).rows
        self.assertEqual([(row.item.pk, row.balances[0]) for row in rows],
                         [("AN-ITEM", Decimal("6")), ("EMPTY-ITEM", Decimal("0"))])
        row = self.report(**filters, brand=self.brand, warehouse=self.stores).rows[0]
        self.assertEqual(row.balances, (Decimal("4"),))
        self.assertEqual(self.report(**filters, warehouse_type=self.main_type).rows[0].balances,
                         (Decimal("4"),))
        self.assertEqual(self.report(**filters, warehouse=self.root).rows[0].balances,
                         (Decimal("6"),))
        other_company = Company.objects.create(name="Other", abbr="OA",
                                                country=self.company.country,
                                                default_currency=self.company.default_currency,
                                                enable_perpetual_inventory=False)
        foreign = Warehouse.objects.create(warehouse_name="Foreign", company=other_company)
        with self.assertRaises(ValidationError):
            self.report(**filters, warehouse=foreign)

    def test_page_csv_permissions_and_invalid_frequency(self):
        self.post("RECEIPT-JAN", date(2026, 1, 2), "4", "5")
        with self.assertRaises(ValidationError):
            self.report(from_date=date(2026, 1, 1), to_date=date(2026, 1, 31),
                        frequency="Daily")
        url = reverse("stock_analytics_report")
        params = {"company": self.company.pk, "from_date": "2026-01-01",
                  "to_date": "2026-02-28", "frequency": "Monthly", "measure": "Value",
                  "item": self.item.pk}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        page = self.client.get(url, params)
        self.assertContains(page, "AN-ITEM")
        self.assertContains(page, 'id="stock-analytics-chart-data"')
        self.assertContains(page, 'class="chart-item"')
        response = self.client.get(url, params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(rows[0][-2:], ["Jan 2026", "Feb 2026"])
        self.assertEqual(rows[1][-2:], ["20.000000000", "20.000000000"])
