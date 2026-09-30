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

from .entries import submit_stock_entry
from .ledger import StockLedgerLine, post_stock_entries
from .models import StockEntry, StockEntryDetail, StockEntryType, Warehouse, WarehouseType
from .stock_ageing_report import stock_ageing_report


class StockAgeingReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Ageing Company", abbr="AC", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.warehouse_type = WarehouseType.objects.create(name="Main")
        cls.root = Warehouse.objects.create(warehouse_name="All", company=cls.company,
                                            is_group=True)
        cls.stores = Warehouse.objects.create(warehouse_name="Stores", company=cls.company,
                                               parent_warehouse=cls.root,
                                               warehouse_type=cls.warehouse_type)
        cls.secondary = Warehouse.objects.create(warehouse_name="Secondary", company=cls.company,
                                                  parent_warehouse=cls.root)
        uom = UnitOfMeasure.objects.create(name="Nos")
        group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        cls.brand = Brand.objects.create(name="Ageing Brand")
        cls.item = Item.objects.create(name="AGE-ITEM", item_group=group, stock_uom=uom,
                                       brand=cls.brand)
        cls.transfer_type = StockEntryType.objects.create(
            name="Material Transfer", purpose=StockEntryType.Purpose.MATERIAL_TRANSFER,
        )
        FiscalYear.objects.create(year="2026", year_start_date=date(2026, 1, 1),
                                  year_end_date=date(2026, 12, 31))

    def post(self, name, day, quantity, rate=None, warehouse=None, item=None):
        return post_stock_entries(
            company=self.company, posting_date=date(2026, 1, day),
            posting_time=time(9), voucher_type="Stock Entry", voucher_no=name,
            lines=[StockLedgerLine(item=item or self.item, warehouse=warehouse or self.stores,
                                   quantity=Decimal(quantity),
                                   incoming_rate=Decimal(rate) if rate is not None else None)],
        )[0]

    def report(self, **filters):
        return stock_ageing_report(company=self.company, to_date=date(2026, 1, 31),
                                   **filters)

    def transfer(self, name, day, quantity, *, source=None, target=None):
        source = source or self.stores
        target = target or self.secondary
        entry = StockEntry.objects.create(
            name=name, company=self.company, stock_entry_type=self.transfer_type,
            posting_date=date(2026, 1, day), posting_time=time(9),
            from_warehouse=source, to_warehouse=target,
        )
        StockEntryDetail.objects.create(
            stock_entry=entry, position=1, item=self.item, source_warehouse=source,
            target_warehouse=target, qty=Decimal(quantity), uom=self.item.stock_uom,
            conversion_factor=Decimal("1"),
        )
        return submit_stock_entry(entry)

    def test_fifo_buckets_and_historical_date(self):
        self.post("RECEIPT-1", 1, "10", "5")
        self.post("RECEIPT-2", 20, "4", "8")
        self.post("ISSUE-3", 25, "-6")
        row = self.report(age_ranges="10,20,30").rows[0]
        self.assertEqual((row.available_qty, row.average_age, row.earliest_age,
                          row.latest_age), (Decimal("8"), Decimal("20.50"), 30, 11))
        self.assertEqual(row.bucket_quantities,
                         (Decimal("0"), Decimal("4"), Decimal("4"), Decimal("0")))
        self.assertEqual(row.bucket_values,
                         (Decimal("0"), Decimal("32"), Decimal("20"), Decimal("0")))
        earlier = stock_ageing_report(company=self.company, to_date=date(2026, 1, 21))
        self.assertEqual(earlier.rows[0].available_qty, Decimal("14"))
        self.assertEqual(earlier.rows[0].average_age, Decimal("14.57"))

    def test_lifo_moving_average_and_warehouse_filter(self):
        self.company.valuation_method = Company.ValuationMethod.LIFO
        self.company.save()
        self.post("RECEIPT-1", 1, "10", "5")
        self.post("RECEIPT-2", 20, "4", "8")
        self.post("ISSUE-3", 25, "-6")
        self.post("RECEIPT-4", 30, "2", "7", warehouse=self.secondary)
        rows = self.report(brand=self.brand, warehouse=self.root, age_ranges="15",
                           show_warehouse_wise_stock=True).rows
        self.assertEqual([(row.warehouse.pk, row.available_qty) for row in rows],
                         [(self.secondary.pk, Decimal("2")), (self.stores.pk, Decimal("8"))])
        self.assertEqual(rows[1].earliest_age, 30)
        self.assertEqual(rows[1].bucket_values[1], Decimal("40"))
        self.assertEqual(len(self.report(warehouse_type=self.warehouse_type).rows), 1)
        self.assertEqual(self.report().rows[0].available_qty, Decimal("10"))

    def test_moving_average_values_follow_ledger(self):
        self.company.valuation_method = Company.ValuationMethod.MOVING_AVERAGE
        self.company.save()
        self.post("RECEIPT-1", 1, "10", "5")
        self.post("RECEIPT-2", 20, "10", "7")
        self.post("ISSUE-3", 25, "-5")
        row = self.report(age_ranges="15").rows[0]
        self.assertEqual(row.bucket_quantities, (Decimal("10"), Decimal("5")))
        self.assertEqual(row.bucket_values, (Decimal("60"), Decimal("30")))

    def test_transfer_preserves_fifo_receipt_dates_with_destination_filter(self):
        self.post("RECEIPT-1", 1, "10", "5")
        self.post("RECEIPT-2", 20, "4", "8")
        self.transfer("TRANSFER-3", 25, "12")
        destination = self.report(warehouse=self.secondary, age_ranges="15").rows[0]
        self.assertEqual((destination.available_qty, destination.average_age,
                          destination.earliest_age, destination.latest_age),
                         (Decimal("12"), Decimal("26.83"), 30, 11))
        self.assertEqual(destination.bucket_quantities, (Decimal("2"), Decimal("10")))
        self.assertEqual(destination.bucket_values, (Decimal("16"), Decimal("50")))
        source = self.report(warehouse=self.stores).rows[0]
        self.assertEqual((source.available_qty, source.average_age),
                         (Decimal("2"), Decimal("11.00")))
        self.assertEqual(self.report().rows[0].available_qty, Decimal("14"))

    def test_lifo_transfer_and_following_issue_preserve_layer_order(self):
        self.company.valuation_method = Company.ValuationMethod.LIFO
        self.company.save()
        self.post("RECEIPT-1", 1, "10", "5")
        self.post("RECEIPT-2", 20, "5", "8")
        self.transfer("TRANSFER-3", 25, "12")
        self.post("ISSUE-4", 28, "-4", warehouse=self.secondary)
        destination = self.report(warehouse=self.secondary, age_ranges="15").rows[0]
        self.assertEqual(destination.bucket_quantities, (Decimal("1"), Decimal("7")))
        self.assertEqual(destination.bucket_values, (Decimal("8"), Decimal("35")))
        self.assertEqual(destination.earliest_age, 30)

    def test_moving_average_transfer_keeps_age_and_ledger_value(self):
        self.company.valuation_method = Company.ValuationMethod.MOVING_AVERAGE
        self.company.save()
        self.post("RECEIPT-1", 1, "10", "5")
        self.post("RECEIPT-2", 20, "10", "7")
        self.transfer("TRANSFER-3", 25, "5")
        destination = self.report(warehouse=self.secondary, age_ranges="15").rows[0]
        self.assertEqual(destination.bucket_quantities, (Decimal("0"), Decimal("5")))
        self.assertEqual(destination.bucket_values, (Decimal("0"), Decimal("30")))
        self.assertEqual(destination.average_age, Decimal("30.00"))

    def test_chart_shows_ten_oldest_items_and_hides_warehouse_mode(self):
        for day in range(1, 12):
            item = Item.objects.create(name=f"AGE-{day:02}", item_group=self.item.item_group,
                                       stock_uom=self.item.stock_uom)
            self.post(f"RECEIPT-{day}", day, "1", "5", item=item)
        chart = self.report().chart_bars
        self.assertEqual(len(chart), 10)
        self.assertEqual((chart[0].item_code, chart[0].average_age, chart[0].width_percent),
                         ("AGE-01", Decimal("30"), Decimal("100")))
        self.assertEqual(chart[-1].item_code, "AGE-10")
        self.assertEqual(self.report(show_warehouse_wise_stock=True).chart_bars, ())

    def test_validation_page_csv_and_permission(self):
        self.post("RECEIPT-1", 1, "10", "5")
        for ranges in ("", "30,20", "1.5,30", "-1,30"):
            with self.assertRaises(ValidationError):
                self.report(age_ranges=ranges)
        other_company = Company.objects.create(
            name="Other", abbr="OT", country=self.company.country,
            default_currency=self.company.default_currency, enable_perpetual_inventory=False,
        )
        foreign = Warehouse.objects.create(warehouse_name="Foreign", company=other_company)
        with self.assertRaises(ValidationError):
            self.report(warehouse=foreign)
        url = reverse("stock_ageing_report")
        params = {"company": self.company.pk, "to_date": "2026-01-31", "age_ranges": "15,30"}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        page = self.client.get(url, params)
        self.assertContains(page, "AGE-ITEM")
        self.assertContains(page, "Oldest 10 items by average age")
        warehouse_page = self.client.get(url, params | {"show_warehouse_wise_stock": "on"})
        self.assertNotContains(warehouse_page, "Oldest 10 items by average age")
        response = self.client.get(url, params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(rows[0][7:11],
                         ["Age 0-15 Qty", "Age 0-15 Value", "Age 16-30 Qty", "Age 16-30 Value"])
        self.assertEqual(rows[1][5:8], ["10.000000000", "30.00", "0"])
