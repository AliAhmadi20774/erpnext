from datetime import date, time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounting.models import FiscalYear
from catalog.models import Item, ItemGroup, UnitOfMeasure
from geo.models import Country, Currency
from organizations.models import Company

from .entries import submit_stock_entry
from .models import Bin, StockEntry, StockEntryDetail, StockEntryType, StockLedgerEntry, Warehouse


class StockEntryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        country = Country.objects.create(name="Iran", code="IR")
        currency = Currency.objects.create(name="IRR")
        cls.company = Company.objects.create(
            name="Stock Entry Company",
            abbr="SEC",
            country=country,
            default_currency=currency,
            enable_perpetual_inventory=False,
        )
        cls.stores = Warehouse.objects.create(
            warehouse_name="Stores", company=cls.company
        )
        cls.finished = Warehouse.objects.create(
            warehouse_name="Finished Goods", company=cls.company
        )
        cls.uom = UnitOfMeasure.objects.create(name="Nos")
        cls.box = UnitOfMeasure.objects.create(name="Box")
        root = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        group = ItemGroup.objects.create(name="Products", parent_item_group=root)
        cls.item = Item.objects.create(
            name="STOCK-001", item_group=group, stock_uom=cls.uom
        )
        conversion = cls.item.uom_conversions.create(
            uom=cls.box, conversion_factor=Decimal("5")
        )
        cls.fiscal_year = FiscalYear.objects.create(
            year="2026",
            year_start_date=date(2026, 1, 1),
            year_end_date=date(2026, 12, 31),
        )
        cls.receipt_type = StockEntryType.objects.create(
            name="Material Receipt", purpose=StockEntryType.Purpose.MATERIAL_RECEIPT
        )
        cls.issue_type = StockEntryType.objects.create(
            name="Material Issue", purpose=StockEntryType.Purpose.MATERIAL_ISSUE
        )
        cls.transfer_type = StockEntryType.objects.create(
            name="Material Transfer", purpose=StockEntryType.Purpose.MATERIAL_TRANSFER
        )

    def make_entry(self, entry_type, *, name, day=1, **fields):
        return StockEntry.objects.create(
            name=name,
            company=self.company,
            stock_entry_type=entry_type,
            posting_date=date(2026, 1, day),
            posting_time=time(9),
            **fields,
        )

    def add_row(self, entry, *, position=1, qty="1", rate="0", uom=None,
                factor="1", source=None, target=None, allow_zero=False):
        return StockEntryDetail.objects.create(
            stock_entry=entry,
            position=position,
            item=self.item,
            source_warehouse=source,
            target_warehouse=target,
            qty=Decimal(qty),
            uom=uom or self.uom,
            conversion_factor=Decimal(factor),
            basic_rate=Decimal(rate),
            allow_zero_valuation_rate=allow_zero,
        )

    def receipt(self, *, name="RECEIPT-001", qty="10", rate="5", day=1):
        entry = self.make_entry(
            self.receipt_type, name=name, day=day, to_warehouse=self.stores
        )
        row = self.add_row(entry, qty=qty, rate=rate)
        return submit_stock_entry(entry), row

    def test_receipt_and_issue_submit_to_ledger(self):
        receipt, receipt_row = self.receipt()
        self.assertEqual(receipt.status, StockEntry.Status.SUBMITTED)
        self.assertEqual(receipt.purpose, StockEntryType.Purpose.MATERIAL_RECEIPT)
        self.assertEqual(receipt.total_incoming_value, Decimal("50"))
        receipt_row.refresh_from_db()
        self.assertEqual(receipt_row.actual_qty, Decimal("10"))
        self.assertEqual(receipt_row.transfer_qty, Decimal("10"))

        issue = self.make_entry(
            self.issue_type, name="ISSUE-001", from_warehouse=self.stores
        )
        issue_row = self.add_row(issue, qty="3")
        issue = submit_stock_entry(issue)
        issue_row.refresh_from_db()
        item_bin = Bin.objects.get(item=self.item, warehouse=self.stores)
        self.assertEqual(item_bin.actual_qty, Decimal("7"))
        self.assertEqual(item_bin.stock_value, Decimal("35"))
        self.assertEqual(issue.total_outgoing_value, Decimal("15"))
        self.assertEqual(issue.value_difference, Decimal("-15"))
        self.assertEqual(issue_row.basic_rate, Decimal("5"))
        self.assertEqual(StockLedgerEntry.objects.filter(voucher_no=issue.name).count(), 1)

        with self.assertRaises(ValidationError):
            submit_stock_entry(issue)
        with self.assertRaises(ValidationError):
            issue.delete()
        issue_row.qty = Decimal("4")
        with self.assertRaises(ValidationError):
            issue_row.save()

    def test_transfer_uses_actual_fifo_outgoing_rate(self):
        self.receipt()
        self.receipt(name="RECEIPT-002", qty="5", rate="8")
        transfer = self.make_entry(
            self.transfer_type,
            name="TRANSFER-001",
            from_warehouse=self.stores,
            to_warehouse=self.finished,
        )
        row = self.add_row(transfer, qty="12")
        transfer = submit_stock_entry(transfer)
        row.refresh_from_db()

        source = Bin.objects.get(item=self.item, warehouse=self.stores)
        target = Bin.objects.get(item=self.item, warehouse=self.finished)
        self.assertEqual(source.actual_qty, Decimal("3"))
        self.assertEqual(source.stock_value, Decimal("24"))
        self.assertEqual(target.actual_qty, Decimal("12"))
        self.assertEqual(target.stock_value, Decimal("66"))
        self.assertEqual(row.basic_rate, Decimal("5.5"))
        self.assertEqual(transfer.total_incoming_value, Decimal("66"))
        self.assertEqual(transfer.total_outgoing_value, Decimal("66"))
        self.assertEqual(transfer.value_difference, Decimal("0"))
        rows = list(StockLedgerEntry.objects.filter(voucher_no=transfer.name))
        self.assertEqual(len(rows), 2)
        incoming = next(entry for entry in rows if entry.actual_qty > 0)
        self.assertEqual(incoming.incoming_rate, Decimal("5.5"))
        self.assertEqual(
            incoming.dependant_sle_voucher_detail_no, f"{row.pk}:OUT"
        )

    def test_transfer_preserves_exact_value_across_fifo_layers(self):
        self.receipt(name="LAYER-001", qty="1", rate="1")
        self.receipt(name="LAYER-002", qty="2", rate="2")
        transfer = self.make_entry(
            self.transfer_type,
            name="LAYER-TRANSFER",
            from_warehouse=self.stores,
            to_warehouse=self.finished,
        )
        self.add_row(transfer, qty="3")
        submitted = submit_stock_entry(transfer)

        target = Bin.objects.get(item=self.item, warehouse=self.finished)
        self.assertEqual(target.stock_value, Decimal("5"))
        self.assertEqual(submitted.value_difference, Decimal("0"))
        incoming = StockLedgerEntry.objects.get(
            voucher_no=transfer.name, actual_qty__gt=0
        )
        self.assertEqual(
            incoming.stock_queue,
            [["1.000000000", "1.000000000"], ["2.000000000", "2.000000000"]],
        )

    def test_transfer_respects_lifo_layers(self):
        self.company.valuation_method = Company.ValuationMethod.LIFO
        self.company.save(update_fields=("valuation_method",))
        self.receipt(name="LIFO-RECEIPT-001", qty="10", rate="5")
        self.receipt(name="LIFO-RECEIPT-002", qty="5", rate="8")
        transfer = self.make_entry(
            self.transfer_type,
            name="LIFO-TRANSFER",
            from_warehouse=self.stores,
            to_warehouse=self.finished,
        )
        self.add_row(transfer, qty="12")
        submitted = submit_stock_entry(transfer)
        self.assertEqual(submitted.total_incoming_value, Decimal("75"))
        self.assertEqual(submitted.total_outgoing_value, Decimal("75"))
        self.assertEqual(
            Bin.objects.get(item=self.item, warehouse=self.finished).stock_value,
            Decimal("75"),
        )

    def test_transfer_respects_moving_average(self):
        self.company.valuation_method = Company.ValuationMethod.MOVING_AVERAGE
        self.company.save(update_fields=("valuation_method",))
        self.receipt(name="MA-RECEIPT-001", qty="10", rate="5")
        self.receipt(name="MA-RECEIPT-002", qty="10", rate="7")
        transfer = self.make_entry(
            self.transfer_type,
            name="MA-TRANSFER",
            from_warehouse=self.stores,
            to_warehouse=self.finished,
        )
        self.add_row(transfer, qty="5")
        submitted = submit_stock_entry(transfer)
        self.assertEqual(submitted.value_difference, Decimal("0"))
        self.assertEqual(
            Bin.objects.get(item=self.item, warehouse=self.finished).stock_value,
            Decimal("30"),
        )

    def test_uom_conversion_and_quantity_validation(self):
        receipt = self.make_entry(
            self.receipt_type, name="BOX-001", to_warehouse=self.stores
        )
        row = self.add_row(
            receipt, qty="2", rate="4", uom=self.box, factor="5"
        )
        submit_stock_entry(receipt)
        row.refresh_from_db()
        self.assertEqual(row.transfer_qty, Decimal("10"))
        self.assertEqual(Bin.objects.get(item=self.item).stock_value, Decimal("40"))

        invalid = self.make_entry(
            self.receipt_type, name="BOX-002", to_warehouse=self.stores
        )
        with self.assertRaises(ValidationError):
            self.add_row(invalid, qty="2", rate="4", uom=self.box, factor="4")
        with self.assertRaises(ValidationError):
            self.add_row(invalid, qty="0", rate="4")

    def test_atomic_failure_keeps_draft_and_original_balance(self):
        self.receipt(qty="2")
        issue = self.make_entry(
            self.issue_type, name="ISSUE-OVER", from_warehouse=self.stores
        )
        self.add_row(issue, position=1, qty="1")
        self.add_row(issue, position=2, qty="2")
        with self.assertRaises(ValidationError):
            submit_stock_entry(issue)

        issue.refresh_from_db()
        self.assertEqual(issue.status, StockEntry.Status.DRAFT)
        self.assertEqual(StockLedgerEntry.objects.filter(voucher_no=issue.name).count(), 0)
        self.assertEqual(Bin.objects.get(item=self.item).actual_qty, Decimal("2"))

    def test_unsupported_purpose_and_perpetual_inventory_are_blocked(self):
        manufacture_type = StockEntryType.objects.create(
            name="Manufacture", purpose=StockEntryType.Purpose.MANUFACTURE
        )
        with self.assertRaises(ValidationError):
            self.make_entry(manufacture_type, name="MANUFACTURE-001")

        receipt = self.make_entry(
            self.receipt_type, name="PERPETUAL-001", to_warehouse=self.stores
        )
        self.add_row(receipt, qty="1", rate="5")
        self.company.enable_perpetual_inventory = True
        self.company.save(update_fields=("enable_perpetual_inventory",))
        with self.assertRaises(ValidationError):
            submit_stock_entry(receipt)
        self.assertFalse(StockLedgerEntry.objects.exists())

    def test_wrong_warehouse_shape_and_zero_rate(self):
        receipt = self.make_entry(
            self.receipt_type, name="ZERO-001", to_warehouse=self.stores
        )
        with self.assertRaises(ValidationError):
            self.add_row(receipt, rate="0")
        row = self.add_row(receipt, rate="0", allow_zero=True)
        submit_stock_entry(receipt)
        self.assertEqual(Bin.objects.get(item=self.item).stock_value, Decimal("0"))

        issue = self.make_entry(self.issue_type, name="BAD-ISSUE")
        with self.assertRaises(ValidationError):
            self.add_row(issue, source=self.stores, target=self.finished)
