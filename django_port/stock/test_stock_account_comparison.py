import csv
import io
from datetime import date, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounting.ledger import LedgerLine, post_gl_entries
from accounting.models import Account, CostCenter, FiscalYear
from catalog.models import Item, ItemGroup, UnitOfMeasure
from geo.models import Country, Currency
from organizations.models import Company

from .entries import submit_stock_entry
from .ledger import StockLedgerLine, post_stock_entries
from .models import ReceiptRateCorrection, StockEntry, StockEntryDetail, StockEntryType, Warehouse
from .repost import cancel_stock_entry, submit_receipt_rate_correction
from .stock_account_comparison import stock_account_comparison


class StockAccountComparisonTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Comparison Company", abbr="CC", country=country,
            default_currency=currency, enable_perpetual_inventory=False,
        )
        cls.stores = Warehouse.objects.create(warehouse_name="Stores", company=cls.company)
        cls.finished = Warehouse.objects.create(warehouse_name="Finished", company=cls.company)
        asset_root = Account.objects.create(
            name="Assets - CC", account_name="Assets", company=cls.company,
            is_group=True, root_type="Asset",
        )
        cls.stock_account = Account.objects.create(
            name="Stores Stock - CC", account_name="Stores Stock", company=cls.company,
            parent_account=asset_root, account_type="Stock",
        )
        cls.finished_account = Account.objects.create(
            name="Finished Stock - CC", account_name="Finished Stock", company=cls.company,
            parent_account=asset_root, account_type="Stock",
        )
        expense_root = Account.objects.create(
            name="Expenses - CC", account_name="Expenses", company=cls.company,
            is_group=True, root_type="Expense",
        )
        cls.adjustment = Account.objects.create(
            name="Adjustment - CC", account_name="Adjustment", company=cls.company,
            parent_account=expense_root,
        )
        center_root = CostCenter.objects.create(
            name="Comparison Company - CC", cost_center_name="Comparison Company",
            company=cls.company, is_group=True,
        )
        cls.center = CostCenter.objects.create(
            name="Main - CC", cost_center_name="Main", company=cls.company,
            parent_cost_center=center_root,
        )
        cls.stores.account = cls.stock_account
        cls.stores.save()
        cls.finished.account = cls.finished_account
        cls.finished.save()
        cls.company.default_inventory_account = cls.stock_account
        cls.company.stock_adjustment_account = cls.adjustment
        cls.company.cost_center = cls.center
        cls.company.enable_perpetual_inventory = True
        cls.company.save()
        cls.uom = UnitOfMeasure.objects.create(name="Nos")
        group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        cls.item = Item.objects.create(name="CMP-ITEM", item_group=group, stock_uom=cls.uom)
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

    def stock_entry(self, name, day, qty, *, rate=None):
        receipt = rate is not None
        entry = StockEntry.objects.create(
            name=name, company=self.company,
            stock_entry_type=self.receipt_type if receipt else self.issue_type,
            posting_date=date(2026, 1, day), posting_time=time(9),
            to_warehouse=self.stores if receipt else None,
            from_warehouse=None if receipt else self.stores,
        )
        detail = StockEntryDetail.objects.create(
            stock_entry=entry, position=1, item=self.item,
            target_warehouse=self.stores if receipt else None,
            source_warehouse=None if receipt else self.stores,
            qty=Decimal(qty), uom=self.uom, conversion_factor=Decimal("1"),
            basic_rate=Decimal(rate) if receipt else Decimal("0"),
        )
        return submit_stock_entry(entry), detail

    def report(self, day=3, **filters):
        return stock_account_comparison(
            company=self.company, as_on_date=date(2026, 1, day), **filters,
        )

    def test_submitted_entries_rate_replay_and_cancellation_balance(self):
        receipt, detail = self.stock_entry("RECEIPT-1", 1, "10", rate="5")
        issue, _ = self.stock_entry("ISSUE-2", 2, "4")
        self.assertEqual(self.report().rows, ())
        correction = ReceiptRateCorrection.objects.create(
            stock_entry_detail=detail, new_rate=Decimal("8"), reason="Supplier price correction",
        )
        submit_receipt_rate_correction(correction)
        self.assertEqual(self.report().rows, ())
        cancel_stock_entry(issue)
        self.assertEqual(self.report().rows, ())
        self.assertEqual(self.report(day=1).rows, ())

    def test_stock_only_gl_only_account_and_date_filters(self):
        self.stock_entry("RECEIPT-1", 1, "10", rate="5")
        post_stock_entries(
            company=self.company, posting_date=date(2026, 1, 2),
            posting_time=time(9), voucher_type="Stock Entry", voucher_no="STOCK-ONLY",
            lines=[StockLedgerLine(
                item=self.item, warehouse=self.stores, quantity=Decimal("1"),
                incoming_rate=Decimal("5"),
            )],
        )
        post_gl_entries(
            company=self.company, posting_date=date(2026, 1, 3),
            voucher_type="Journal Entry", voucher_no="GL-ONLY",
            lines=[LedgerLine(account=self.stock_account, debit=Decimal("3")),
                   LedgerLine(account=self.adjustment, credit=Decimal("3"),
                              cost_center=self.center)],
        )
        self.assertEqual(self.report(day=1).rows, ())
        rows = self.report().rows
        self.assertEqual([(row.voucher_no, row.ledger_type, row.difference_value)
                          for row in rows],
                         [("STOCK-ONLY", "Stock Ledger Entry", Decimal("5")),
                          ("GL-ONLY", "GL Entry", Decimal("-3"))])
        self.assertEqual([row.voucher_no for row in self.report(
            from_date=date(2026, 1, 3),
        ).rows], ["GL-ONLY"])
        self.assertEqual(self.report(account=self.stock_account).rows, rows)
        self.assertEqual(self.report(account=self.finished_account).rows, ())
        with self.assertRaises(ValidationError):
            self.report(account=self.adjustment)
        with self.assertRaises(ValidationError):
            self.report(from_date=date(2026, 1, 4))

    def test_page_csv_and_permission(self):
        self.stock_entry("RECEIPT-1", 1, "10", rate="5")
        url = reverse("stock_account_comparison_report")
        params = {"company": self.company.pk, "as_on_date": "2026-01-03"}
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
        self.assertEqual(rows[0][:4], ["Ledger Type", "Posting Date", "Voucher Type", "Voucher Number"])
        self.assertEqual(len(rows), 1)
