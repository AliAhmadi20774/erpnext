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

from .ledger import StockLedgerLine, post_stock_entries
from .models import Bin, ItemReorder, Warehouse
from .stock_projected_qty import stock_projected_qty


class StockProjectedQtyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Projected Company", abbr="PC", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.other_company = Company.objects.create(
            name="Other Company", abbr="OC", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.root_warehouse = Warehouse.objects.create(
            warehouse_name="All Warehouses", company=cls.company, is_group=True,
        )
        cls.stores = Warehouse.objects.create(
            warehouse_name="Stores", company=cls.company,
            parent_warehouse=cls.root_warehouse,
        )
        cls.other_stores = Warehouse.objects.create(
            warehouse_name="Other Stores", company=cls.other_company,
        )
        uom = UnitOfMeasure.objects.create(name="Nos")
        root_group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        cls.group = ItemGroup.objects.create(
            name="Projected Group", parent_item_group=root_group, is_group=True,
        )
        other_group = ItemGroup.objects.create(name="Other Group", parent_item_group=root_group)
        cls.item = Item.objects.create(name="PROJ-1", item_group=cls.group, stock_uom=uom)
        cls.other_item = Item.objects.create(name="PROJ-2", item_group=other_group, stock_uom=uom)
        FiscalYear.objects.create(
            year="2026", year_start_date=date(2026, 1, 1),
            year_end_date=date(2026, 12, 31),
        )

    def post(self, name, *, item, company, warehouse, qty):
        post_stock_entries(
            company=company, posting_date=date(2026, 1, 1), posting_time=time(9),
            voucher_type="Stock Entry", voucher_no=name,
            lines=[StockLedgerLine(
                item=item, warehouse=warehouse, quantity=Decimal(qty),
                incoming_rate=Decimal("5"),
            )],
        )

    def seed_bins(self):
        self.post("RECEIPT-1", item=self.item, company=self.company,
                  warehouse=self.stores, qty="10")
        self.post("RECEIPT-2", item=self.item, company=self.other_company,
                  warehouse=self.other_stores, qty="4")
        self.post("RECEIPT-3", item=self.other_item, company=self.company,
                  warehouse=self.stores, qty="2")

    def test_projected_components_and_filters(self):
        self.seed_bins()
        item_bin = Bin.objects.get(item=self.item, warehouse=self.stores)
        item_bin.planned_qty = Decimal("3")
        item_bin.indented_qty = Decimal("4")
        item_bin.ordered_qty = Decimal("5")
        item_bin.reserved_qty = Decimal("2")
        item_bin.reserved_qty_for_production = Decimal("1")
        item_bin.reserved_qty_for_production_plan = Decimal("1")
        item_bin.reserved_qty_for_sub_contract = Decimal("1")
        item_bin.reserved_stock = Decimal("2")
        item_bin.save(_allow_stock_write=True)
        ItemReorder.objects.create(
            item=self.item, warehouse=self.stores, warehouse_group=self.root_warehouse,
            warehouse_reorder_level=Decimal("18"), warehouse_reorder_qty=Decimal("6"),
        )

        rows = stock_projected_qty(company=self.company, item_group=self.group,
                                   warehouse=self.root_warehouse).rows
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.bin.item_id, self.item.pk)
        self.assertEqual(row.bin.projected_qty, Decimal("17"))
        self.assertEqual(row.bin.reserved_stock, Decimal("2"))
        self.assertEqual((row.reorder_level, row.reorder_qty, row.shortage_qty),
                         (Decimal("18"), Decimal("6"), Decimal("1")))
        self.assertEqual([r.bin.item_id for r in stock_projected_qty(
            company=self.company, item=self.other_item,
        ).rows], [self.other_item.pk])
        other_company_row = next(row for row in stock_projected_qty(item=self.item).rows
                                 if row.bin.warehouse_id == self.other_stores.pk)
        self.assertEqual(other_company_row.shortage_qty, Decimal("0"))
        self.assertEqual(len(stock_projected_qty(item=self.item).rows), 2)

        with self.assertRaises(ValidationError):
            stock_projected_qty(company=self.company, warehouse=self.other_stores)

    def test_excludes_disabled_and_expired_items(self):
        self.seed_bins()
        self.other_item.disabled = True
        self.other_item.save(update_fields=["disabled"])
        self.assertEqual([row.bin.item_id for row in stock_projected_qty(company=self.company).rows],
                         [self.item.pk])
        self.item.end_of_life = date(2020, 1, 1)
        self.item.save(update_fields=["end_of_life"])
        self.assertEqual(stock_projected_qty(company=self.company).rows, ())

    def test_page_csv_and_permission(self):
        self.seed_bins()
        ItemReorder.objects.create(
            item=self.item, warehouse=self.stores,
            warehouse_reorder_level=Decimal("12"), warehouse_reorder_qty=Decimal("5"),
        )
        url = reverse("stock_projected_qty_report")
        params = {"company": self.company.pk, "item": self.item.pk,
                  "warehouse": self.root_warehouse.pk}
        self.assertEqual(self.client.get(url, params).status_code, 302)
        viewer = get_user_model().objects.create_user(username="viewer", password="test-password")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com",
        )
        self.client.force_login(admin)
        self.assertContains(self.client.get(url, params), "PROJ-1")
        response = self.client.get(url, params | {"format": "csv"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(rows[0][-3:], ["Reorder Level", "Reorder Qty", "Shortage Qty"])
        self.assertEqual(rows[1][0], "PROJ-1")
        self.assertEqual(rows[1][-4:],
                         ["10.000000000", "12.000000000", "5.000000000", "2.000000000"])

    def test_reorder_setting_validation(self):
        setting = ItemReorder.objects.create(
            item=self.item, warehouse=self.stores,
            warehouse_reorder_level=Decimal("5"), warehouse_reorder_qty=Decimal("3"),
        )
        self.assertEqual(setting.material_request_type, ItemReorder.MaterialRequestType.PURCHASE)
        with self.assertRaises(ValidationError):
            ItemReorder.objects.create(item=self.item, warehouse=self.stores, position=2)
        with self.assertRaises(ValidationError):
            ItemReorder.objects.create(item=self.item, warehouse=self.root_warehouse, position=2)
        with self.assertRaises(ValidationError):
            ItemReorder.objects.create(
                item=self.item, warehouse=self.other_stores,
                warehouse_group=self.root_warehouse, position=2,
            )
        with self.assertRaises(ValidationError):
            ItemReorder.objects.create(
                item=self.item, warehouse=self.other_stores,
                warehouse_reorder_level=Decimal("-1"), position=2,
            )
