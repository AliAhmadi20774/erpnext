from datetime import date, time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounting.models import Account, CostCenter, FiscalYear, GLEntry
from accounting.periods import create_accounting_period
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

    def enable_perpetual(self):
        asset_root = Account.objects.create(
            name="Assets - SEC", account_name="Assets", company=self.company,
            is_group=True, root_type="Asset",
        )
        source_stock = Account.objects.create(
            name="Stores Stock - SEC", account_name="Stores Stock",
            company=self.company, parent_account=asset_root, account_type="Stock",
        )
        target_stock = Account.objects.create(
            name="Finished Stock - SEC", account_name="Finished Stock",
            company=self.company, parent_account=asset_root, account_type="Stock",
        )
        expense_root = Account.objects.create(
            name="Expenses - SEC", account_name="Expenses", company=self.company,
            is_group=True, root_type="Expense",
        )
        adjustment = Account.objects.create(
            name="Stock Adjustment - SEC", account_name="Stock Adjustment",
            company=self.company, parent_account=expense_root,
        )
        root_center = CostCenter.objects.create(
            name="Stock Entry Company - SEC", cost_center_name=self.company.name,
            company=self.company, is_group=True,
        )
        center = CostCenter.objects.create(
            name="Main - SEC", cost_center_name="Main", company=self.company,
            parent_cost_center=root_center,
        )
        self.stores.account = source_stock
        self.stores.save()
        self.finished.account = target_stock
        self.finished.save()
        self.company.default_inventory_account = source_stock
        self.company.stock_adjustment_account = adjustment
        self.company.cost_center = center
        self.company.enable_perpetual_inventory = True
        self.company.save()
        return source_stock, target_stock, adjustment, center

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

    def test_unsupported_purpose_and_missing_perpetual_accounts_are_blocked(self):
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

    def test_perpetual_receipt_and_issue_post_balanced_gl(self):
        source_stock, _, adjustment, center = self.enable_perpetual()
        receipt, _ = self.receipt()
        receipt_gl = list(GLEntry.objects.filter(voucher_no=receipt.name))
        self.assertEqual(len(receipt_gl), 2)
        self.assertEqual(
            [(row.account_id, row.debit, row.credit) for row in receipt_gl],
            [
                (source_stock.pk, Decimal("50"), Decimal("0")),
                (adjustment.pk, Decimal("0"), Decimal("50")),
            ],
        )
        self.assertTrue(all(row.cost_center_id == center.pk for row in receipt_gl))
        adjustment.disabled = True
        with self.assertRaises(ValidationError):
            adjustment.save()
        adjustment.refresh_from_db()

        issue = self.make_entry(
            self.issue_type, name="PERPETUAL-ISSUE", from_warehouse=self.stores
        )
        self.add_row(issue, qty="3")
        submit_stock_entry(issue)
        issue_gl = list(GLEntry.objects.filter(voucher_no=issue.name))
        self.assertEqual(
            [(row.account_id, row.debit, row.credit) for row in issue_gl],
            [
                (adjustment.pk, Decimal("15"), Decimal("0")),
                (source_stock.pk, Decimal("0"), Decimal("15")),
            ],
        )
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("35"))

    def test_perpetual_transfer_moves_value_between_warehouse_accounts(self):
        source_stock, target_stock, _, _ = self.enable_perpetual()
        self.receipt()
        transfer = self.make_entry(
            self.transfer_type, name="PERPETUAL-TRANSFER",
            from_warehouse=self.stores, to_warehouse=self.finished,
        )
        self.add_row(transfer, qty="4")
        submit_stock_entry(transfer)
        gl_rows = list(GLEntry.objects.filter(voucher_no=transfer.name))
        self.assertEqual(
            [(row.account_id, row.debit, row.credit) for row in gl_rows],
            [
                (target_stock.pk, Decimal("20"), Decimal("0")),
                (source_stock.pk, Decimal("0"), Decimal("20")),
            ],
        )
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).stock_value, Decimal("20"))

    def test_perpetual_transfer_on_same_account_needs_no_gl_rows(self):
        source_stock, _, _, _ = self.enable_perpetual()
        self.receipt()
        self.finished.account = source_stock
        self.finished.save()
        transfer = self.make_entry(
            self.transfer_type, name="SAME-ACCOUNT-TRANSFER",
            from_warehouse=self.stores, to_warehouse=self.finished,
        )
        self.add_row(transfer, qty="4")
        submit_stock_entry(transfer)
        self.assertEqual(GLEntry.objects.filter(voucher_no=transfer.name).count(), 0)
        self.assertEqual(StockLedgerEntry.objects.filter(voucher_no=transfer.name).count(), 2)

    def test_missing_difference_account_rolls_back_stock_and_gl(self):
        self.enable_perpetual()
        self.company.stock_adjustment_account = None
        self.company.save(update_fields=("stock_adjustment_account",))
        receipt = self.make_entry(
            self.receipt_type, name="MISSING-ADJUSTMENT", to_warehouse=self.stores
        )
        self.add_row(receipt, qty="2", rate="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(receipt)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, StockEntry.Status.DRAFT)
        self.assertFalse(Bin.objects.exists())
        self.assertFalse(StockLedgerEntry.objects.exists())
        self.assertFalse(GLEntry.objects.exists())

    def test_missing_cost_center_rolls_back_profit_and_loss_posting(self):
        self.enable_perpetual()
        self.company.cost_center = None
        self.company.save(update_fields=("cost_center",))
        receipt = self.make_entry(
            self.receipt_type, name="MISSING-CENTER", to_warehouse=self.stores
        )
        self.add_row(receipt, qty="2", rate="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(receipt)
        self.assertFalse(Bin.objects.exists())
        self.assertFalse(StockLedgerEntry.objects.exists())
        self.assertFalse(GLEntry.objects.exists())

    def test_row_difference_account_overrides_company_default(self):
        source_stock, _, adjustment, center = self.enable_perpetual()
        alternative = Account.objects.create(
            name="Other Stock Expense - SEC", account_name="Other Stock Expense",
            company=self.company, parent_account=adjustment.parent_account,
        )
        receipt = self.make_entry(
            self.receipt_type, name="ROW-DIFFERENCE", to_warehouse=self.stores
        )
        row = self.add_row(receipt, qty="2", rate="5")
        row.expense_account = alternative
        row.cost_center = center
        row.save()
        submit_stock_entry(receipt)
        self.assertEqual(
            set(GLEntry.objects.filter(voucher_no=receipt.name).values_list("account_id", flat=True)),
            {source_stock.pk, alternative.pk},
        )

    def test_foreign_currency_inventory_account_rolls_back(self):
        self.enable_perpetual()
        foreign_currency = Currency.objects.create(name="USD")
        foreign_stock = Account.objects.create(
            name="Foreign Stock - SEC", account_name="Foreign Stock",
            company=self.company,
            parent_account=self.company.default_inventory_account.parent_account,
            account_type="Stock", account_currency=foreign_currency,
        )
        self.stores.account = foreign_stock
        self.stores.save()
        receipt = self.make_entry(
            self.receipt_type, name="FOREIGN-STOCK", to_warehouse=self.stores
        )
        self.add_row(receipt, qty="2", rate="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(receipt)
        self.assertFalse(Bin.objects.exists())
        self.assertFalse(StockLedgerEntry.objects.exists())
        self.assertFalse(GLEntry.objects.exists())

    def test_opening_receipt_requires_balance_sheet_difference_account(self):
        self.enable_perpetual()
        opening = self.make_entry(
            self.receipt_type, name="OPENING-STOCK", to_warehouse=self.stores,
            is_opening=True,
        )
        row = self.add_row(opening, qty="2", rate="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(opening)
        self.assertFalse(StockLedgerEntry.objects.exists())

        temporary = Account.objects.create(
            name="Temporary Opening - SEC", account_name="Temporary Opening",
            company=self.company, parent_account=self.company.default_inventory_account.parent_account,
        )
        row.expense_account = temporary
        row.save()
        submit_stock_entry(opening)
        self.assertTrue(
            all(GLEntry.objects.filter(voucher_no=opening.name).values_list("is_opening", flat=True))
        )

    def test_closed_stock_accounting_period_rejects_before_posting(self):
        self.enable_perpetual()
        create_accounting_period(
            period_name="January 2026", company=self.company,
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        receipt = self.make_entry(
            self.receipt_type, name="CLOSED-STOCK", to_warehouse=self.stores
        )
        self.add_row(receipt, qty="2", rate="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(receipt)
        self.assertFalse(StockLedgerEntry.objects.exists())
        self.assertFalse(GLEntry.objects.exists())

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
