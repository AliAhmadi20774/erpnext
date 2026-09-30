from datetime import date, time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounting.ledger import account_balance
from accounting.models import Account, CostCenter, FiscalYear, GLEntry
from accounting.periods import create_accounting_period
from catalog.models import Item, ItemGroup, UnitOfMeasure
from geo.models import Country, Currency
from organizations.models import Company

from .entries import submit_stock_entry
from .ledger import StockLedgerLine, post_stock_entries
from .reconciliation import cancel_stock_reconciliation, submit_stock_reconciliation
from .repost import cancel_stock_entry, submit_receipt_rate_correction
from .models import (
    Bin, ReceiptRateCorrection, StockEntry, StockEntryDetail, StockEntryType,
    StockLedgerEntry, StockReconciliation, StockReconciliationItem, Warehouse,
)


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

    def test_cancel_latest_receipt_reverses_gl_and_keeps_audit_rows(self):
        self.enable_perpetual()
        receipt, row = self.receipt()
        cancelled = cancel_stock_entry(receipt)
        self.assertEqual(cancelled.status, StockEntry.Status.CANCELLED)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("0"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("0"))
        self.assertTrue(StockLedgerEntry.objects.get(voucher_no=receipt.pk).is_cancelled)
        original = list(GLEntry.objects.filter(voucher_type="Stock Entry", voucher_no=receipt.pk))
        reversal = list(GLEntry.objects.filter(voucher_type="Stock Entry Cancellation", voucher_no=receipt.pk))
        self.assertEqual(len(reversal), 2)
        self.assertEqual(
            sorted((line.account_id, line.debit, line.credit) for line in reversal),
            sorted((line.account_id, line.credit, line.debit) for line in original),
        )
        with self.assertRaises(ValidationError):
            cancel_stock_entry(receipt)
        with self.assertRaises(ValidationError):
            receipt.delete()
        with self.assertRaises(ValidationError):
            row.delete()

    def test_cancel_old_issue_replays_fifo_and_posts_gl_delta(self):
        self.enable_perpetual()
        self.receipt(name="FIFO-A", qty="10", rate="5", day=1)
        self.receipt(name="FIFO-B", qty="10", rate="8", day=2)
        first = self.make_entry(self.issue_type, name="FIFO-ISSUE-A", day=3, from_warehouse=self.stores)
        self.add_row(first, qty="10")
        submit_stock_entry(first)
        later = self.make_entry(self.issue_type, name="FIFO-ISSUE-B", day=4, from_warehouse=self.stores)
        later_row = self.add_row(later, qty="5")
        later = submit_stock_entry(later)
        self.assertEqual(later.total_outgoing_value, Decimal("40"))

        cancel_stock_entry(first)
        later.refresh_from_db()
        later_row.refresh_from_db()
        item_bin = Bin.objects.get(item=self.item, warehouse=self.stores)
        self.assertEqual((item_bin.actual_qty, item_bin.stock_value), (Decimal("15"), Decimal("105")))
        self.assertEqual(later.total_outgoing_value, Decimal("25"))
        self.assertEqual(later_row.basic_rate, Decimal("5"))
        self.assertEqual(StockLedgerEntry.objects.get(voucher_no=later.pk).stock_value_difference, Decimal("-25"))
        correction = GLEntry.objects.filter(voucher_type="Stock Valuation Repost")
        self.assertEqual(correction.count(), 2)
        self.assertEqual(sum((line.debit for line in correction), Decimal("0")), Decimal("15"))
        self.assertEqual(sum((line.credit for line in correction), Decimal("0")), Decimal("15"))

    def test_cancel_receipt_that_later_issue_needs_rolls_back(self):
        receipt, _ = self.receipt()
        issue = self.make_entry(self.issue_type, name="NEEDS-STOCK", day=2, from_warehouse=self.stores)
        self.add_row(issue, qty="8")
        submit_stock_entry(issue)
        with self.assertRaises(ValidationError):
            cancel_stock_entry(receipt)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, StockEntry.Status.SUBMITTED)
        self.assertFalse(StockLedgerEntry.objects.get(voucher_no=receipt.pk).is_cancelled)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("2"))

    def test_cancel_transfer_replays_target_and_reverses_transfer_gl(self):
        self.enable_perpetual()
        self.receipt()
        transfer = self.make_entry(
            self.transfer_type, name="CANCEL-TRANSFER", day=2,
            from_warehouse=self.stores, to_warehouse=self.finished,
        )
        self.add_row(transfer, qty="4")
        submit_stock_entry(transfer)
        cancel_stock_entry(transfer)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("10"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).actual_qty, Decimal("0"))
        self.assertEqual(StockLedgerEntry.objects.filter(voucher_no=transfer.pk, is_cancelled=True).count(), 2)
        self.assertEqual(GLEntry.objects.filter(voucher_type="Stock Entry Cancellation", voucher_no=transfer.pk).count(), 2)

    def test_cancel_in_closed_period_is_rejected(self):
        receipt, _ = self.receipt()
        create_accounting_period(
            period_name="Closed January", company=self.company,
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        with self.assertRaises(ValidationError):
            cancel_stock_entry(receipt)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, StockEntry.Status.SUBMITTED)

    def test_repeated_cancellations_use_existing_repost_gl(self):
        self.enable_perpetual()
        self.receipt(name="REPLAY-RECEIPT-A", qty="10", rate="5", day=1)
        self.receipt(name="REPLAY-RECEIPT-B", qty="10", rate="8", day=2)
        first = self.make_entry(self.issue_type, name="REPLAY-ISSUE-A", day=3, from_warehouse=self.stores)
        self.add_row(first, qty="10")
        submit_stock_entry(first)
        second = self.make_entry(self.issue_type, name="REPLAY-ISSUE-B", day=4, from_warehouse=self.stores)
        self.add_row(second, qty="5")
        submit_stock_entry(second)
        cancel_stock_entry(first)
        self.assertEqual(GLEntry.objects.filter(voucher_type="Stock Valuation Repost").count(), 2)
        cancel_stock_entry(second)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("130"))
        self.assertEqual(GLEntry.objects.filter(voucher_type="Stock Entry Cancellation").count(), 6)
        self.assertEqual(
            account_balance(self.company.default_inventory_account),
            Decimal("130"),
        )

    def test_cancel_moving_average_then_post_uses_active_ledger(self):
        self.company.valuation_method = Company.ValuationMethod.MOVING_AVERAGE
        self.company.save()
        first, _ = self.receipt(name="AVG-FIRST", qty="10", rate="5", day=1)
        self.receipt(name="AVG-SECOND", qty="10", rate="9", day=2)
        cancel_stock_entry(first)
        item_bin = Bin.objects.get(item=self.item, warehouse=self.stores)
        self.assertEqual((item_bin.actual_qty, item_bin.stock_value), (Decimal("10"), Decimal("90")))
        issue = self.make_entry(self.issue_type, name="AVG-AFTER-CANCEL", day=3, from_warehouse=self.stores)
        self.add_row(issue, qty="2")
        issue = submit_stock_entry(issue)
        self.assertEqual(issue.total_outgoing_value, Decimal("18"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("72"))

    def test_cancel_rejects_closed_later_revaluation_period(self):
        self.receipt(name="CLOSED-REPLAY-A", qty="10", rate="5", day=1)
        self.receipt(name="CLOSED-REPLAY-B", qty="10", rate="8", day=2)
        first = self.make_entry(self.issue_type, name="CLOSED-REPLAY-ISSUE", day=3, from_warehouse=self.stores)
        self.add_row(first, qty="10")
        submit_stock_entry(first)
        later = self.make_entry(self.issue_type, name="CLOSED-REPLAY-LATER", day=4, from_warehouse=self.stores)
        self.add_row(later, qty="5")
        submit_stock_entry(later)
        create_accounting_period(
            period_name="Closed replay date", company=self.company,
            start_date=date(2026, 1, 4), end_date=date(2026, 1, 4),
        )
        with self.assertRaises(ValidationError):
            cancel_stock_entry(first)
        first.refresh_from_db()
        self.assertEqual(first.status, StockEntry.Status.SUBMITTED)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("40"))

    def test_cancel_rejects_unrelated_direct_ledger_voucher(self):
        receipt, _ = self.receipt()
        post_stock_entries(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(9),
            voucher_type="Stock Reconciliation", voucher_no="UNSUPPORTED-001",
            lines=(StockLedgerLine(
                item=self.item, warehouse=self.stores, quantity=Decimal("1"),
                incoming_rate=Decimal("5"),
            ),),
        )
        with self.assertRaises(ValidationError):
            cancel_stock_entry(receipt)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, StockEntry.Status.SUBMITTED)

    def test_backdated_receipt_revalues_later_fifo_issue_and_gl(self):
        self.enable_perpetual()
        self.receipt(name="LATER-RECEIPT", qty="10", rate="8", day=2)
        issue = self.make_entry(self.issue_type, name="LATER-ISSUE", day=3, from_warehouse=self.stores)
        issue_row = self.add_row(issue, qty="5")
        submit_stock_entry(issue)
        earlier, _ = self.receipt(name="EARLIER-RECEIPT", qty="10", rate="5", day=1)
        issue.refresh_from_db()
        issue_row.refresh_from_db()
        item_bin = Bin.objects.get(item=self.item, warehouse=self.stores)
        self.assertEqual(earlier.total_incoming_value, Decimal("50"))
        self.assertEqual((item_bin.actual_qty, item_bin.stock_value), (Decimal("15"), Decimal("105")))
        self.assertEqual((issue.total_outgoing_value, issue_row.basic_rate), (Decimal("25"), Decimal("5")))
        self.assertEqual(GLEntry.objects.filter(voucher_type="Stock Valuation Repost").count(), 2)
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("105"))

    def test_backdated_issue_revalues_later_fifo_issue(self):
        self.enable_perpetual()
        self.receipt(name="ISSUE-STOCK-A", qty="10", rate="5", day=1)
        self.receipt(name="ISSUE-STOCK-B", qty="10", rate="8", day=2)
        later = self.make_entry(self.issue_type, name="ISSUE-LATER", day=4, from_warehouse=self.stores)
        self.add_row(later, qty="10")
        submit_stock_entry(later)
        earlier = self.make_entry(self.issue_type, name="ISSUE-EARLIER", day=3, from_warehouse=self.stores)
        self.add_row(earlier, qty="5")
        earlier = submit_stock_entry(earlier)
        later.refresh_from_db()
        self.assertEqual(earlier.total_outgoing_value, Decimal("25"))
        self.assertEqual(later.total_outgoing_value, Decimal("65"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("40"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("40"))

    def test_backdated_issue_that_makes_later_stock_negative_rolls_back(self):
        self.receipt(name="NEGATIVE-STOCK", qty="10", rate="5", day=1)
        later = self.make_entry(self.issue_type, name="NEGATIVE-LATER", day=3, from_warehouse=self.stores)
        self.add_row(later, qty="8")
        submit_stock_entry(later)
        earlier = self.make_entry(self.issue_type, name="NEGATIVE-EARLIER", day=2, from_warehouse=self.stores)
        self.add_row(earlier, qty="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(earlier)
        earlier.refresh_from_db()
        self.assertEqual(earlier.status, StockEntry.Status.DRAFT)
        self.assertFalse(StockLedgerEntry.objects.filter(voucher_no=earlier.pk).exists())
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("2"))

    def test_backdated_transfer_revalues_target_issue(self):
        self.enable_perpetual()
        self.receipt(name="SOURCE-STOCK", qty="10", rate="5", day=1)
        target_receipt = self.make_entry(
            self.receipt_type, name="TARGET-STOCK", day=3, to_warehouse=self.finished,
        )
        self.add_row(target_receipt, qty="10", rate="8")
        submit_stock_entry(target_receipt)
        later = self.make_entry(self.issue_type, name="TARGET-ISSUE", day=4, from_warehouse=self.finished)
        self.add_row(later, qty="5")
        submit_stock_entry(later)
        transfer = self.make_entry(
            self.transfer_type, name="EARLY-TRANSFER", day=2,
            from_warehouse=self.stores, to_warehouse=self.finished,
        )
        self.add_row(transfer, qty="5")
        transfer = submit_stock_entry(transfer)
        later.refresh_from_db()
        self.assertEqual(transfer.total_outgoing_value, Decimal("25"))
        self.assertEqual(transfer.total_incoming_value, Decimal("25"))
        self.assertEqual(later.total_outgoing_value, Decimal("25"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).stock_value, Decimal("80"))
        self.assertEqual(account_balance(self.finished.account), Decimal("80"))

    def test_backdated_receipt_rejects_closed_later_period(self):
        self.receipt(name="CLOSED-LATER-RECEIPT", qty="10", rate="8", day=2)
        create_accounting_period(
            period_name="Closed future stock", company=self.company,
            start_date=date(2026, 1, 2), end_date=date(2026, 1, 2),
        )
        earlier = self.make_entry(self.receipt_type, name="CLOSED-EARLIER", day=1, to_warehouse=self.stores)
        self.add_row(earlier, qty="10", rate="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(earlier)
        self.assertFalse(StockLedgerEntry.objects.filter(voucher_no=earlier.pk).exists())
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("80"))

    def test_backdated_issue_revalues_later_lifo_issue(self):
        self.company.valuation_method = Company.ValuationMethod.LIFO
        self.company.save()
        self.receipt(name="LIFO-EARLY-A", qty="10", rate="5", day=1)
        self.receipt(name="LIFO-EARLY-B", qty="10", rate="8", day=2)
        later = self.make_entry(self.issue_type, name="LIFO-LATER", day=4, from_warehouse=self.stores)
        self.add_row(later, qty="10")
        submit_stock_entry(later)
        earlier = self.make_entry(self.issue_type, name="LIFO-BACKDATED", day=3, from_warehouse=self.stores)
        self.add_row(earlier, qty="5")
        earlier = submit_stock_entry(earlier)
        later.refresh_from_db()
        self.assertEqual(earlier.total_outgoing_value, Decimal("40"))
        self.assertEqual(later.total_outgoing_value, Decimal("65"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("25"))

    def test_backdated_receipt_revalues_moving_average_issue(self):
        self.company.valuation_method = Company.ValuationMethod.MOVING_AVERAGE
        self.company.save()
        self.receipt(name="AVERAGE-LATER-STOCK", qty="10", rate="8", day=2)
        issue = self.make_entry(self.issue_type, name="AVERAGE-LATER-ISSUE", day=3, from_warehouse=self.stores)
        self.add_row(issue, qty="5")
        submit_stock_entry(issue)
        self.receipt(name="AVERAGE-EARLY-STOCK", qty="10", rate="5", day=1)
        issue.refresh_from_db()
        self.assertEqual(issue.total_outgoing_value, Decimal("32.5"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("97.5"))

    def test_cancel_backdated_receipt_replays_future_gl_again(self):
        self.enable_perpetual()
        self.receipt(name="CANCEL-REPLAY-LATER", qty="10", rate="8", day=2)
        issue = self.make_entry(self.issue_type, name="CANCEL-REPLAY-ISSUE", day=3, from_warehouse=self.stores)
        self.add_row(issue, qty="5")
        submit_stock_entry(issue)
        earlier, _ = self.receipt(name="CANCEL-REPLAY-EARLY", qty="10", rate="5", day=1)
        cancel_stock_entry(earlier)
        issue.refresh_from_db()
        self.assertEqual(issue.total_outgoing_value, Decimal("40"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("40"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("40"))

    def test_low_level_deferred_posting_cannot_be_requested_directly(self):
        with self.assertRaises(ValidationError):
            post_stock_entries(
                company=self.company, posting_date=date(2026, 1, 1), posting_time=time(9),
                voucher_type="Stock Entry", voucher_no="NO-DIRECT-DEFER",
                lines=(StockLedgerLine(
                    item=self.item, warehouse=self.stores, quantity=Decimal("1"),
                    incoming_rate=Decimal("5"),
                ),), _defer_replay=True,
            )
        self.assertFalse(StockLedgerEntry.objects.exists())

    def test_backdated_entry_rejects_unsupported_stock_voucher(self):
        post_stock_entries(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(9),
            voucher_type="Stock Reconciliation", voucher_no="OTHER-STOCK-DOC",
            lines=(StockLedgerLine(
                item=self.item, warehouse=self.stores, quantity=Decimal("10"),
                incoming_rate=Decimal("5"),
            ),),
        )
        earlier = self.make_entry(self.receipt_type, name="BEFORE-OTHER-DOC", day=1, to_warehouse=self.stores)
        self.add_row(earlier, qty="1", rate="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(earlier)
        self.assertFalse(StockLedgerEntry.objects.filter(voucher_no=earlier.pk).exists())
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("10"))

    def test_receipt_rate_correction_replays_fifo_issue_and_gl(self):
        self.enable_perpetual()
        receipt, row = self.receipt(name="RATE-RECEIPT", qty="10", rate="5", day=1)
        issue = self.make_entry(self.issue_type, name="RATE-ISSUE", day=2, from_warehouse=self.stores)
        self.add_row(issue, qty="5")
        submit_stock_entry(issue)
        correction = ReceiptRateCorrection.objects.create(
            stock_entry_detail=row, new_rate=Decimal("8"), reason="Correct supplier price",
        )
        correction = submit_receipt_rate_correction(correction)
        receipt.refresh_from_db()
        row.refresh_from_db()
        issue.refresh_from_db()
        self.assertEqual((correction.previous_rate, correction.status), (
            Decimal("5"), ReceiptRateCorrection.Status.SUBMITTED,
        ))
        self.assertEqual((receipt.total_incoming_value, row.basic_rate), (Decimal("80"), Decimal("8")))
        self.assertEqual(issue.total_outgoing_value, Decimal("40"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("40"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("40"))
        self.assertEqual(GLEntry.objects.filter(voucher_type="Stock Valuation Repost").count(), 4)
        with self.assertRaises(ValidationError):
            submit_receipt_rate_correction(correction)
        correction.reason = "Changed after posting"
        with self.assertRaises(ValidationError):
            correction.save()
        with self.assertRaises(ValidationError):
            correction.delete()

    def test_receipt_rate_correction_propagates_through_transfer(self):
        self.enable_perpetual()
        _, row = self.receipt(name="RATE-SOURCE", qty="10", rate="5", day=1)
        transfer = self.make_entry(
            self.transfer_type, name="RATE-TRANSFER", day=2,
            from_warehouse=self.stores, to_warehouse=self.finished,
        )
        self.add_row(transfer, qty="5")
        submit_stock_entry(transfer)
        issue = self.make_entry(self.issue_type, name="RATE-TARGET-ISSUE", day=3, from_warehouse=self.finished)
        self.add_row(issue, qty="2")
        submit_stock_entry(issue)
        correction = ReceiptRateCorrection.objects.create(
            stock_entry_detail=row, new_rate=Decimal("8"), reason="Inventory invoice adjustment",
        )
        submit_receipt_rate_correction(correction)
        transfer.refresh_from_db()
        issue.refresh_from_db()
        self.assertEqual(transfer.total_outgoing_value, Decimal("40"))
        self.assertEqual(transfer.total_incoming_value, Decimal("40"))
        self.assertEqual(issue.total_outgoing_value, Decimal("16"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).stock_value, Decimal("24"))
        self.assertEqual(account_balance(self.finished.account), Decimal("24"))

    def test_receipt_rate_correction_nonperpetual_is_audited(self):
        _, row = self.receipt(name="RATE-NONPERPETUAL", qty="10", rate="5")
        correction = ReceiptRateCorrection.objects.create(
            stock_entry_detail=row, new_rate=Decimal("7"), reason="Manual valuation correction",
        )
        submit_receipt_rate_correction(correction)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("70"))
        self.assertEqual(GLEntry.objects.count(), 0)
        self.assertEqual(ReceiptRateCorrection.objects.get(pk=correction.pk).previous_rate, Decimal("5"))

    def test_receipt_rate_correction_closed_future_period_rolls_back(self):
        _, row = self.receipt(name="RATE-CLOSED", qty="10", rate="5", day=1)
        issue = self.make_entry(self.issue_type, name="RATE-CLOSED-ISSUE", day=2, from_warehouse=self.stores)
        self.add_row(issue, qty="5")
        submit_stock_entry(issue)
        create_accounting_period(
            period_name="Closed rate future", company=self.company,
            start_date=date(2026, 1, 2), end_date=date(2026, 1, 2),
        )
        correction = ReceiptRateCorrection.objects.create(
            stock_entry_detail=row, new_rate=Decimal("8"), reason="Needs approval",
        )
        with self.assertRaises(ValidationError):
            submit_receipt_rate_correction(correction)
        correction.refresh_from_db()
        self.assertEqual(correction.status, ReceiptRateCorrection.Status.DRAFT)
        self.assertEqual(StockLedgerEntry.objects.get(voucher_no="RATE-CLOSED").incoming_rate, Decimal("5"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("25"))

    def test_receipt_rate_correction_requires_new_rate_and_reason(self):
        _, row = self.receipt(name="RATE-VALIDATION")
        with self.assertRaises(ValidationError):
            ReceiptRateCorrection.objects.create(
                stock_entry_detail=row, new_rate=Decimal("-1"), reason="Invalid",
            )
        with self.assertRaises(ValidationError):
            ReceiptRateCorrection.objects.create(
                stock_entry_detail=row, new_rate=Decimal("7"), reason=" ",
            )
        same = ReceiptRateCorrection.objects.create(
            stock_entry_detail=row, new_rate=Decimal("5"), reason="No change",
        )
        with self.assertRaises(ValidationError):
            submit_receipt_rate_correction(same)

    def test_multiple_rate_corrections_then_cancel_reverse_all_gl(self):
        self.enable_perpetual()
        receipt, row = self.receipt(name="RATE-CHAIN", qty="10", rate="5")
        for rate in ("8", "6"):
            submit_receipt_rate_correction(ReceiptRateCorrection.objects.create(
                stock_entry_detail=row, new_rate=Decimal(rate),
                reason=f"Price amended to {rate}",
            ))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("60"))
        self.assertEqual(
            list(ReceiptRateCorrection.objects.order_by("id").values_list("previous_rate", flat=True)),
            [Decimal("5"), Decimal("8")],
        )
        cancel_stock_entry(receipt)
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("0"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("0"))

    def test_stock_reconciliation_increase_posts_receipt_and_gl(self):
        self.enable_perpetual()
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 1), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("5"), receipt_rate=Decimal("8"),
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        row.refresh_from_db()
        self.assertEqual(reconciliation.status, StockReconciliation.Status.SUBMITTED)
        self.assertEqual((row.previous_qty, row.difference_qty), (Decimal("0"), Decimal("5")))
        self.assertIsNotNone(reconciliation.receipt_entry_id)
        self.assertIsNone(reconciliation.issue_entry_id)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("40"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("40"))
        self.assertEqual(StockLedgerEntry.objects.get(voucher_no=reconciliation.receipt_entry_id).voucher_type, "Stock Entry")

    def test_stock_reconciliation_mixed_increase_and_decrease(self):
        self.enable_perpetual()
        self.receipt(name="RECO-START", qty="10", rate="5", day=1)
        target = self.make_entry(self.receipt_type, name="RECO-TARGET-START", day=1, to_warehouse=self.finished)
        self.add_row(target, qty="2", rate="8")
        submit_stock_entry(target)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("6"),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=2, item=self.item,
            warehouse=self.finished, counted_qty=Decimal("5"), receipt_rate=Decimal("8"),
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        self.assertEqual((reconciliation.total_increase_qty, reconciliation.total_decrease_qty),
                         (Decimal("3"), Decimal("4")))
        self.assertIsNotNone(reconciliation.receipt_entry_id)
        self.assertIsNotNone(reconciliation.issue_entry_id)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("6"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).actual_qty, Decimal("5"))
        self.assertEqual(account_balance(self.stores.account), Decimal("30"))
        self.assertEqual(account_balance(self.finished.account), Decimal("40"))
        cancel_stock_reconciliation(reconciliation)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("10"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).actual_qty, Decimal("2"))
        self.assertEqual(account_balance(self.stores.account), Decimal("50"))
        self.assertEqual(account_balance(self.finished.account), Decimal("16"))

    def test_stock_reconciliation_cancel_uses_source_voucher(self):
        self.enable_perpetual()
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 1), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("5"), receipt_rate=Decimal("8"),
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        with self.assertRaises(ValidationError):
            cancel_stock_entry(reconciliation.receipt_entry)
        cancelled = cancel_stock_reconciliation(reconciliation)
        self.assertEqual(cancelled.status, StockReconciliation.Status.CANCELLED)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("0"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("0"))
        with self.assertRaises(ValidationError):
            reconciliation.delete()

    def test_stock_reconciliation_rejects_unchanged_and_missing_rate(self):
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 1), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("0"),
        )
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        row.counted_qty = Decimal("5")
        row.save()
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        reconciliation.refresh_from_db()
        self.assertEqual(reconciliation.status, StockReconciliation.Status.DRAFT)
        self.assertFalse(StockLedgerEntry.objects.exists())

    def test_stock_reconciliation_backdated_increase_uses_as_of_balance(self):
        self.enable_perpetual()
        self.receipt(name="RECO-FUTURE", qty="10", rate="5", day=3)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("6"), receipt_rate=Decimal("8"),
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        row.refresh_from_db()
        self.assertEqual((row.previous_qty, row.difference_qty, row.value_difference),
                         (Decimal("0"), Decimal("6"), Decimal("48")))
        self.assertEqual(reconciliation.total_value_difference, Decimal("48"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("16"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("98"))
        cancel_stock_reconciliation(reconciliation)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("10"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("50"))

    def test_stock_reconciliation_backdated_decrease_revalues_future_issue_and_gl(self):
        self.enable_perpetual()
        self.receipt(name="COUNT-EARLY", qty="10", rate="5", day=1)
        self.receipt(name="COUNT-LATER", qty="10", rate="8", day=3)
        future = self.make_entry(self.issue_type, name="COUNT-FUTURE-ISSUE", day=4,
                                 from_warehouse=self.stores)
        self.add_row(future, qty="5")
        submit_stock_entry(future)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("6"),
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        row.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual((row.previous_qty, row.previous_stock_value,
                          row.difference_qty, row.value_difference),
                         (Decimal("10"), Decimal("50"), Decimal("-4"), Decimal("-20")))
        self.assertEqual(future.total_outgoing_value, Decimal("25"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("85"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("85"))

    def test_stock_reconciliation_backdated_negative_future_rolls_back(self):
        self.receipt(name="COUNT-ROLLBACK-BASE", qty="10", rate="5", day=1)
        future = self.make_entry(self.issue_type, name="COUNT-ROLLBACK-ISSUE", day=3,
                                 from_warehouse=self.stores)
        self.add_row(future, qty="8")
        submit_stock_entry(future)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("5"),
        )
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        reconciliation.refresh_from_db()
        self.assertEqual(reconciliation.status, StockReconciliation.Status.DRAFT)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("2"))
        self.assertFalse(StockEntry.objects.filter(
            remarks__startswith=f"Stock Reconciliation {reconciliation.pk}"
        ).exists())

    def test_stock_reconciliation_backdated_value_only_replays_future_receipt(self):
        self.receipt(name="COUNT-VALUE-BASE", qty="10", rate="5", day=1)
        self.receipt(name="COUNT-VALUE-FUTURE", qty="5", rate="8", day=3)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("7"),
            revalue_existing_stock=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        row.refresh_from_db()
        self.assertEqual((row.previous_stock_value, row.value_difference),
                         (Decimal("50"), Decimal("20")))
        self.assertEqual(reconciliation.total_value_difference, Decimal("20"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("110"))

    def test_stock_reconciliation_backdated_value_only_revalues_future_fifo_issue_and_gl(self):
        self.enable_perpetual()
        self.receipt(name="VALUE-BACK-FIFO-BASE", qty="10", rate="5", day=1)
        future = self.make_entry(
            self.issue_type, name="VALUE-BACK-FIFO-FUTURE", day=3,
            from_warehouse=self.stores,
        )
        self.add_row(future, qty="4")
        submit_stock_entry(future)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        row.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual((row.previous_qty, row.previous_stock_value,
                          row.difference_qty, row.value_difference),
                         (Decimal("10"), Decimal("50"), Decimal("0"), Decimal("30")))
        self.assertEqual(future.total_outgoing_value, Decimal("32"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("48"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("48"))
        cancel_stock_reconciliation(reconciliation)
        future.refresh_from_db()
        self.assertEqual(future.total_outgoing_value, Decimal("20"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("30"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("30"))

    def test_stock_reconciliation_backdated_value_only_revalues_future_lifo_issue(self):
        self.company.valuation_method = Company.ValuationMethod.LIFO
        self.company.save()
        self.receipt(name="VALUE-BACK-LIFO-A", qty="10", rate="5", day=1)
        self.receipt(name="VALUE-BACK-LIFO-B", qty="10", rate="8", day=2)
        future = self.make_entry(
            self.issue_type, name="VALUE-BACK-LIFO-FUTURE", day=4,
            from_warehouse=self.stores,
        )
        self.add_row(future, qty="5")
        submit_stock_entry(future)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("20"), receipt_rate=Decimal("7"),
            revalue_existing_stock=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        future.refresh_from_db()
        self.assertEqual(reconciliation.total_value_difference, Decimal("10"))
        self.assertEqual(future.total_outgoing_value, Decimal("35"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("105"))

    def test_stock_reconciliation_backdated_value_only_revalues_future_average_issue(self):
        self.company.valuation_method = Company.ValuationMethod.MOVING_AVERAGE
        self.company.save()
        self.receipt(name="VALUE-BACK-AVG-A", qty="10", rate="5", day=1)
        self.receipt(name="VALUE-BACK-AVG-B", qty="10", rate="8", day=2)
        future = self.make_entry(
            self.issue_type, name="VALUE-BACK-AVG-FUTURE", day=4,
            from_warehouse=self.stores,
        )
        self.add_row(future, qty="5")
        submit_stock_entry(future)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("20"), receipt_rate=Decimal("7"),
            revalue_existing_stock=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        future.refresh_from_db()
        self.assertEqual(reconciliation.total_value_difference, Decimal("10"))
        self.assertEqual(future.total_outgoing_value, Decimal("35"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("105"))

    def test_stock_reconciliation_backdated_value_only_with_quantity_increase(self):
        self.enable_perpetual()
        self.receipt(name="VALUE-BACK-MIX-BASE", qty="10", rate="5", day=1)
        future = self.make_entry(
            self.issue_type, name="VALUE-BACK-MIX-FUTURE", day=3,
            from_warehouse=self.stores,
        )
        self.add_row(future, qty="4")
        submit_stock_entry(future)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True,
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=2, item=self.item,
            warehouse=self.finished, counted_qty=Decimal("2"), receipt_rate=Decimal("7"),
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        future.refresh_from_db()
        self.assertEqual(reconciliation.total_value_difference, Decimal("44"))
        self.assertEqual(reconciliation.total_increase_qty, Decimal("2"))
        self.assertEqual(future.total_outgoing_value, Decimal("32"))
        self.assertEqual(account_balance(self.stores.account), Decimal("48"))
        self.assertEqual(account_balance(self.finished.account), Decimal("14"))

    def test_stock_reconciliation_backdated_value_only_closed_future_rolls_back(self):
        self.receipt(name="VALUE-BACK-CLOSED-BASE", qty="10", rate="5", day=1)
        future = self.make_entry(
            self.issue_type, name="VALUE-BACK-CLOSED-FUTURE", day=3,
            from_warehouse=self.stores,
        )
        self.add_row(future, qty="4")
        submit_stock_entry(future)
        create_accounting_period(
            period_name="Closed future value reset", company=self.company,
            start_date=date(2026, 1, 3), end_date=date(2026, 1, 3),
        )
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True,
        )
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        reconciliation.refresh_from_db()
        self.assertEqual(reconciliation.status, StockReconciliation.Status.DRAFT)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("30"))
        self.assertFalse(StockEntry.objects.filter(
            remarks__startswith=f"Stock Reconciliation {reconciliation.pk}"
        ).exists())

    def test_stock_reconciliation_backdated_value_only_refreshes_later_count_value(self):
        self.receipt(name="VALUE-BACK-LATER-BASE", qty="10", rate="5", day=1)
        future = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 4), posting_time=time(10),
        )
        future_row = StockReconciliationItem.objects.create(
            reconciliation=future, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("8"),
        )
        submit_stock_reconciliation(future)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True,
        )
        submit_stock_reconciliation(reconciliation)
        future_row.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual((future_row.previous_stock_value, future_row.value_difference),
                         (Decimal("80"), Decimal("-16")))
        self.assertEqual(future.total_value_difference, Decimal("-16"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("64"))

    def test_stock_reconciliation_backdated_multiple_increases(self):
        self.enable_perpetual()
        self.receipt(name="COUNT-MULTI-FUTURE", qty="5", rate="5", day=3)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        for position, warehouse in enumerate((self.stores, self.finished), 1):
            StockReconciliationItem.objects.create(
                reconciliation=reconciliation, position=position, item=self.item,
                warehouse=warehouse, counted_qty=Decimal("2"), receipt_rate=Decimal("5"),
            )
        reconciliation = submit_stock_reconciliation(reconciliation)
        self.assertEqual(reconciliation.total_increase_qty, Decimal("4"))
        self.assertEqual(reconciliation.total_value_difference, Decimal("20"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("7"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).actual_qty, Decimal("2"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("35"))
        self.assertEqual(account_balance(self.finished.account), Decimal("10"))

    def test_stock_reconciliation_backdated_mixed_rows_revalue_future_transfer(self):
        self.enable_perpetual()
        self.receipt(name="COUNT-MIX-STORE", qty="10", rate="5", day=1)
        finished_base = self.make_entry(
            self.receipt_type, name="COUNT-MIX-FINISHED", day=1,
            to_warehouse=self.finished,
        )
        self.add_row(finished_base, qty="5", rate="8")
        submit_stock_entry(finished_base)
        future = self.make_entry(
            self.transfer_type, name="COUNT-MIX-TRANSFER", day=3,
            from_warehouse=self.stores, to_warehouse=self.finished,
        )
        self.add_row(future, qty="4")
        submit_stock_entry(future)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        stores_row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("8"),
        )
        finished_row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=2, item=self.item,
            warehouse=self.finished, counted_qty=Decimal("8"), receipt_rate=Decimal("7"),
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        stores_row.refresh_from_db()
        finished_row.refresh_from_db()
        self.assertEqual((stores_row.previous_qty, stores_row.difference_qty,
                          stores_row.value_difference),
                         (Decimal("10"), Decimal("-2"), Decimal("-10")))
        self.assertEqual((finished_row.previous_qty, finished_row.difference_qty,
                          finished_row.value_difference),
                         (Decimal("5"), Decimal("3"), Decimal("21")))
        self.assertEqual(reconciliation.total_value_difference, Decimal("11"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("20"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).stock_value, Decimal("81"))
        self.assertEqual(account_balance(self.stores.account), Decimal("20"))
        self.assertEqual(account_balance(self.finished.account), Decimal("81"))
        cancel_stock_reconciliation(reconciliation)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("30"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).stock_value, Decimal("60"))
        self.assertEqual(account_balance(self.stores.account), Decimal("30"))
        self.assertEqual(account_balance(self.finished.account), Decimal("60"))

    def test_stock_reconciliation_backdated_second_entry_failure_rolls_back_both(self):
        self.receipt(name="COUNT-PAIR-STORE", qty="10", rate="5", day=1)
        finished_future = self.make_entry(
            self.receipt_type, name="COUNT-PAIR-FUTURE", day=3,
            to_warehouse=self.finished,
        )
        self.add_row(finished_future, qty="5", rate="8")
        submit_stock_entry(finished_future)
        create_accounting_period(
            period_name="Closed second count replay", company=self.company,
            start_date=date(2026, 1, 3), end_date=date(2026, 1, 3),
        )
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("8"),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=2, item=self.item,
            warehouse=self.finished, counted_qty=Decimal("2"), receipt_rate=Decimal("5"),
        )
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        reconciliation.refresh_from_db()
        self.assertEqual(reconciliation.status, StockReconciliation.Status.DRAFT)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("10"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).actual_qty, Decimal("5"))
        self.assertFalse(StockEntry.objects.filter(
            remarks__startswith=f"Stock Reconciliation {reconciliation.pk}"
        ).exists())

    def test_stock_reconciliation_backdated_rejects_closed_future_period(self):
        self.receipt(name="COUNT-CLOSED-FUTURE", qty="5", rate="5", day=3)
        create_accounting_period(
            period_name="Closed future count replay", company=self.company,
            start_date=date(2026, 1, 3), end_date=date(2026, 1, 3),
        )
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("2"), receipt_rate=Decimal("5"),
        )
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("5"))
        self.assertFalse(StockEntry.objects.filter(
            remarks__startswith=f"Stock Reconciliation {reconciliation.pk}"
        ).exists())

    def test_stock_reconciliation_count_blocks_earlier_replay(self):
        first, _ = self.receipt(name="COUNT-ORIGINAL", qty="5", rate="5", day=1)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("8"), receipt_rate=Decimal("5"),
        )
        submit_stock_reconciliation(reconciliation)
        with self.assertRaises(ValidationError):
            cancel_stock_entry(first)
        earlier = self.make_entry(
            self.receipt_type, name="COUNT-BACKDATED", day=1, to_warehouse=self.stores,
        )
        earlier.posting_time = time(8)
        earlier.save()
        self.add_row(earlier, qty="2", rate="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(earlier)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("8"))
        self.assertFalse(StockLedgerEntry.objects.filter(voucher_no=earlier.pk).exists())

    def test_stock_reconciliation_closed_period_and_rate_only_are_rejected(self):
        self.receipt(name="COUNT-CLOSED-BASE", qty="5", rate="5", day=1)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("5"), receipt_rate=Decimal("8"),
        )
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        row.receipt_rate = Decimal("0")
        row.counted_qty = Decimal("7")
        row.save()
        create_accounting_period(
            period_name="Closed count", company=self.company,
            start_date=date(2026, 1, 2), end_date=date(2026, 1, 2),
        )
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        self.assertEqual(reconciliation.status, StockReconciliation.Status.DRAFT)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("5"))

    def test_direct_value_adjustment_resets_fifo_without_quantity_movement(self):
        self.enable_perpetual()
        self.receipt(name="DIRECT-FIFO-A", qty="10", rate="5", day=1)
        self.receipt(name="DIRECT-FIFO-B", qty="10", rate="8", day=2)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("20"), receipt_rate=Decimal("7"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        row.refresh_from_db()
        adjustment = StockLedgerEntry.objects.get(voucher_no=reconciliation.receipt_entry_id)
        self.assertIsNone(reconciliation.issue_entry_id)
        self.assertEqual(adjustment.voucher_type, "Stock Reconciliation")
        self.assertEqual((adjustment.actual_qty, adjustment.qty_after_transaction,
                          adjustment.stock_value_difference),
                         (Decimal("0"), Decimal("20"), Decimal("10")))
        self.assertTrue(adjustment.is_value_reset)
        self.assertFalse(adjustment.is_adjustment_entry)
        self.assertEqual((row.previous_stock_value, row.value_difference),
                         (Decimal("130"), Decimal("10")))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("140"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("140"))
        future = self.make_entry(self.issue_type, name="DIRECT-FIFO-ISSUE", day=4,
                                 from_warehouse=self.stores)
        self.add_row(future, qty="5")
        future = submit_stock_entry(future)
        self.assertEqual(future.total_outgoing_value, Decimal("35"))
        cancel_stock_reconciliation(reconciliation)
        future.refresh_from_db()
        self.assertEqual(future.total_outgoing_value, Decimal("25"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("105"))

    def test_direct_value_adjustment_reduces_value_and_gl(self):
        _, _, adjustment_account, _ = self.enable_perpetual()
        self.receipt(name="DIRECT-REDUCE-BASE", qty="10", rate="8", day=1)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("5"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        self.assertEqual(reconciliation.total_value_difference, Decimal("-30"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("50"))
        self.assertEqual(account_balance(adjustment_account), Decimal("-50"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("10"))

    def test_direct_value_adjustment_zero_rate_requires_opt_in(self):
        self.enable_perpetual()
        self.receipt(name="DIRECT-ZERO-BASE", qty="10", rate="5", day=1)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("0"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        row.allow_zero_valuation_rate = True
        row.save()
        reconciliation = submit_stock_reconciliation(reconciliation)
        self.assertEqual(reconciliation.total_value_difference, Decimal("-50"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("10"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("0"))

    def test_direct_value_adjustment_resets_lifo_layers(self):
        self.company.valuation_method = Company.ValuationMethod.LIFO
        self.company.save()
        self.receipt(name="DIRECT-LIFO-A", qty="10", rate="5", day=1)
        self.receipt(name="DIRECT-LIFO-B", qty="10", rate="8", day=2)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("20"), receipt_rate=Decimal("7"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        submit_stock_reconciliation(reconciliation)
        future = self.make_entry(self.issue_type, name="DIRECT-LIFO-ISSUE", day=4,
                                 from_warehouse=self.stores)
        self.add_row(future, qty="5")
        future = submit_stock_entry(future)
        self.assertEqual(future.total_outgoing_value, Decimal("35"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("105"))

    def test_direct_value_adjustment_resets_moving_average(self):
        self.company.valuation_method = Company.ValuationMethod.MOVING_AVERAGE
        self.company.save()
        self.receipt(name="DIRECT-AVG-A", qty="10", rate="5", day=1)
        self.receipt(name="DIRECT-AVG-B", qty="10", rate="8", day=2)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("20"), receipt_rate=Decimal("7"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        submit_stock_reconciliation(reconciliation)
        future = self.make_entry(self.issue_type, name="DIRECT-AVG-ISSUE", day=4,
                                 from_warehouse=self.stores)
        self.add_row(future, qty="5")
        future = submit_stock_entry(future)
        self.assertEqual(future.total_outgoing_value, Decimal("35"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("105"))

    def test_direct_value_adjustment_refreshes_after_prior_rate_correction(self):
        self.enable_perpetual()
        _, source_row = self.receipt(name="DIRECT-CORRECT-BASE", qty="10", rate="5", day=1)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        submit_receipt_rate_correction(ReceiptRateCorrection.objects.create(
            stock_entry_detail=source_row, new_rate=Decimal("6"),
            reason="Correct receipt before direct value adjustment",
        ))
        row.refresh_from_db()
        reconciliation.refresh_from_db()
        self.assertEqual((row.previous_stock_value, row.value_difference),
                         (Decimal("60"), Decimal("20")))
        self.assertEqual(reconciliation.total_value_difference, Decimal("20"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("80"))

    def test_direct_value_adjustment_blocks_earlier_quantity_shift(self):
        self.receipt(name="DIRECT-GUARD-BASE", qty="10", rate="5", day=1)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        submit_stock_reconciliation(reconciliation)
        earlier = self.make_entry(self.receipt_type, name="DIRECT-GUARD-EARLIER",
                                  day=1, to_warehouse=self.stores)
        earlier.posting_time = time(8)
        earlier.save()
        self.add_row(earlier, qty="2", rate="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(earlier)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("80"))

    def test_backdated_direct_value_adjustment_revalues_future_issue_and_gl(self):
        self.enable_perpetual()
        self.receipt(name="DIRECT-BACK-BASE", qty="10", rate="5", day=1)
        future = self.make_entry(self.issue_type, name="DIRECT-BACK-ISSUE", day=3,
                                 from_warehouse=self.stores)
        self.add_row(future, qty="4")
        submit_stock_entry(future)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        future.refresh_from_db()
        self.assertEqual(StockLedgerEntry.objects.get(
            voucher_no=reconciliation.receipt_entry_id
        ).voucher_type, "Stock Reconciliation")
        self.assertEqual(future.total_outgoing_value, Decimal("32"))
        self.assertEqual(reconciliation.total_value_difference, Decimal("30"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("48"))
        cancel_stock_reconciliation(reconciliation)
        future.refresh_from_db()
        self.assertEqual(future.total_outgoing_value, Decimal("20"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("30"))

    def test_direct_value_adjustment_mixes_with_quantity_increase(self):
        self.enable_perpetual()
        self.receipt(name="DIRECT-MIX-BASE", qty="10", rate="5", day=1)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=2, item=self.item,
            warehouse=self.finished, counted_qty=Decimal("3"), receipt_rate=Decimal("5"),
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        self.assertEqual(reconciliation.total_value_difference, Decimal("45"))
        self.assertEqual(reconciliation.total_increase_qty, Decimal("3"))
        self.assertEqual(account_balance(self.stores.account), Decimal("80"))
        self.assertEqual(account_balance(self.finished.account), Decimal("15"))
        self.assertEqual(StockLedgerEntry.objects.filter(
            voucher_no=reconciliation.receipt_entry_id
        ).count(), 2)
        self.assertEqual(set(StockLedgerEntry.objects.filter(
            voucher_no=reconciliation.receipt_entry_id
        ).values_list("voucher_type", flat=True)), {"Stock Reconciliation"})
        cancel_stock_reconciliation(reconciliation)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("50"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).stock_value, Decimal("0"))

    def test_backdated_direct_value_adjustment_mixes_with_quantity_increase(self):
        self.enable_perpetual()
        self.receipt(name="DIRECT-BACK-MIX-BASE", qty="10", rate="5", day=1)
        future = self.make_entry(self.issue_type, name="DIRECT-BACK-MIX-ISSUE", day=3,
                                 from_warehouse=self.stores)
        self.add_row(future, qty="4")
        submit_stock_entry(future)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=2, item=self.item,
            warehouse=self.finished, counted_qty=Decimal("3"), receipt_rate=Decimal("5"),
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        future.refresh_from_db()
        self.assertEqual(future.total_outgoing_value, Decimal("32"))
        self.assertEqual(reconciliation.total_value_difference, Decimal("45"))
        self.assertEqual(set(StockLedgerEntry.objects.filter(
            voucher_no=reconciliation.receipt_entry_id
        ).values_list("voucher_type", flat=True)), {"Stock Reconciliation"})
        cancel_stock_reconciliation(reconciliation)
        future.refresh_from_db()
        self.assertEqual(future.total_outgoing_value, Decimal("20"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).actual_qty,
                         Decimal("0"))

    def test_backdated_direct_value_adjustment_closed_future_rolls_back(self):
        self.receipt(name="DIRECT-CLOSED-BASE", qty="10", rate="5", day=1)
        future = self.make_entry(self.issue_type, name="DIRECT-CLOSED-ISSUE", day=3,
                                 from_warehouse=self.stores)
        self.add_row(future, qty="4")
        submit_stock_entry(future)
        create_accounting_period(
            period_name="Closed direct adjustment replay", company=self.company,
            start_date=date(2026, 1, 3), end_date=date(2026, 1, 3),
        )
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True, direct_value_adjustment=True,
        )
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        reconciliation.refresh_from_db()
        self.assertEqual(reconciliation.status, StockReconciliation.Status.DRAFT)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("30"))
        self.assertFalse(StockEntry.objects.filter(
            remarks__startswith=f"Stock Reconciliation {reconciliation.pk}"
        ).exists())

    def test_direct_value_adjustment_rejects_missing_stock_and_invalid_flag(self):
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 1), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("0"),
        )
        row.direct_value_adjustment = True
        with self.assertRaises(ValidationError):
            row.save()
        row.revalue_existing_stock = True
        row.allow_zero_valuation_rate = True
        row.save()
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        self.assertFalse(StockLedgerEntry.objects.exists())

    def test_value_only_reconciliation_resets_fifo_layers_and_gl(self):
        self.enable_perpetual()
        self.receipt(name="VALUE-FIFO-A", qty="10", rate="5", day=1)
        self.receipt(name="VALUE-FIFO-B", qty="10", rate="8", day=2)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("20"), receipt_rate=Decimal("7"),
            revalue_existing_stock=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        row.refresh_from_db()
        self.assertEqual((reconciliation.total_increase_qty, reconciliation.total_decrease_qty),
                         (Decimal("0"), Decimal("0")))
        self.assertEqual(reconciliation.total_value_difference, Decimal("10"))
        self.assertEqual((row.previous_stock_value, row.value_difference),
                         (Decimal("130"), Decimal("10")))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("20"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("140"))
        issue = self.make_entry(self.issue_type, name="VALUE-FIFO-ISSUE", day=4, from_warehouse=self.stores)
        self.add_row(issue, qty="5")
        issue = submit_stock_entry(issue)
        self.assertEqual(issue.total_outgoing_value, Decimal("35"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("105"))
        cancel_stock_reconciliation(reconciliation)
        issue.refresh_from_db()
        self.assertEqual(issue.total_outgoing_value, Decimal("25"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("105"))

    def test_value_only_reconciliation_zero_rate_requires_opt_in(self):
        self.enable_perpetual()
        self.receipt(name="VALUE-ZERO-SOURCE", qty="10", rate="5", day=1)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        row = StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("0"),
            revalue_existing_stock=True,
        )
        with self.assertRaises(ValidationError):
            submit_stock_reconciliation(reconciliation)
        row.allow_zero_valuation_rate = True
        row.save()
        reconciliation = submit_stock_reconciliation(reconciliation)
        self.assertEqual(reconciliation.total_value_difference, Decimal("-50"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("10"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("0"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("0"))
        cancel_stock_reconciliation(reconciliation)
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("50"))

    def test_value_only_reconciliation_moving_average(self):
        self.company.valuation_method = Company.ValuationMethod.MOVING_AVERAGE
        self.company.save()
        self.receipt(name="AVG-VALUE-A", qty="10", rate="5", day=1)
        self.receipt(name="AVG-VALUE-B", qty="10", rate="8", day=2)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("20"), receipt_rate=Decimal("7"),
            revalue_existing_stock=True,
        )
        submit_stock_reconciliation(reconciliation)
        issue = self.make_entry(self.issue_type, name="AVG-VALUE-OUT", day=4, from_warehouse=self.stores)
        self.add_row(issue, qty="5")
        issue = submit_stock_entry(issue)
        self.assertEqual(issue.total_outgoing_value, Decimal("35"))

    def test_value_only_reconciliation_lifo(self):
        self.company.valuation_method = Company.ValuationMethod.LIFO
        self.company.save()
        self.receipt(name="LIFO-VALUE-A", qty="10", rate="5", day=1)
        self.receipt(name="LIFO-VALUE-B", qty="10", rate="8", day=2)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 3), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("20"), receipt_rate=Decimal("7"),
            revalue_existing_stock=True,
        )
        submit_stock_reconciliation(reconciliation)
        issue = self.make_entry(self.issue_type, name="LIFO-VALUE-OUT", day=4, from_warehouse=self.stores)
        self.add_row(issue, qty="5")
        issue = submit_stock_entry(issue)
        self.assertEqual(issue.total_outgoing_value, Decimal("35"))

    def test_value_only_reconciliation_rejects_historical_quantity_shift(self):
        self.receipt(name="VALUE-HISTORY", qty="10", rate="5", day=1)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True,
        )
        submit_stock_reconciliation(reconciliation)
        earlier = self.make_entry(self.receipt_type, name="VALUE-BEFORE", day=1, to_warehouse=self.stores)
        earlier.posting_time = time(8)
        earlier.save()
        self.add_row(earlier, qty="2", rate="5")
        with self.assertRaises(ValidationError):
            submit_stock_entry(earlier)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).actual_qty, Decimal("10"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("80"))

    def test_value_only_reconciliation_accepts_prior_rate_correction(self):
        self.enable_perpetual()
        _, source_row = self.receipt(name="RESET-SOURCE", qty="10", rate="5", day=1)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("8"),
            revalue_existing_stock=True,
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        submit_receipt_rate_correction(ReceiptRateCorrection.objects.create(
            stock_entry_detail=source_row, new_rate=Decimal("6"),
            reason="Correct original receipt after value reset",
        ))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("80"))
        self.assertEqual(account_balance(self.company.default_inventory_account), Decimal("80"))
        reconciliation.refresh_from_db()
        self.assertEqual(reconciliation.total_value_difference, Decimal("20"))

    def test_value_only_reconciliation_mixes_with_quantity_increase(self):
        self.receipt(name="MIXED-VALUE-SOURCE", qty="10", rate="5", day=1)
        target = self.make_entry(self.receipt_type, name="MIXED-VALUE-TARGET", day=1, to_warehouse=self.finished)
        self.add_row(target, qty="2", rate="8")
        submit_stock_entry(target)
        reconciliation = StockReconciliation.objects.create(
            company=self.company, posting_date=date(2026, 1, 2), posting_time=time(10),
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=1, item=self.item,
            warehouse=self.stores, counted_qty=Decimal("10"), receipt_rate=Decimal("7"),
            revalue_existing_stock=True,
        )
        StockReconciliationItem.objects.create(
            reconciliation=reconciliation, position=2, item=self.item,
            warehouse=self.finished, counted_qty=Decimal("5"), receipt_rate=Decimal("8"),
        )
        reconciliation = submit_stock_reconciliation(reconciliation)
        self.assertEqual(reconciliation.total_increase_qty, Decimal("3"))
        self.assertEqual(reconciliation.total_decrease_qty, Decimal("0"))
        self.assertEqual(reconciliation.total_value_difference, Decimal("44"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.stores).stock_value, Decimal("70"))
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.finished).stock_value, Decimal("40"))

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
