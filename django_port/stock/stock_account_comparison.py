"""Compare stock value changes with Stock-account GL movements by voucher."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError

from accounting.models import Account, GLEntry
from organizations.models import Company

from .models import StockLedgerEntry, StockReconciliation, Warehouse


ZERO = Decimal("0")
ADJUSTMENT_VOUCHERS = {
    "Stock Entry Cancellation", "Stock Reconciliation Cancellation",
    "Stock Valuation Repost",
}


@dataclass(frozen=True)
class StockAccountComparisonRow:
    voucher_type: str
    voucher_no: str
    posting_date: date
    stock_value: Decimal
    account_value: Decimal
    has_stock_entry: bool

    @property
    def difference_value(self):
        return self.stock_value - self.account_value

    @property
    def ledger_type(self):
        return "Stock Ledger Entry" if self.has_stock_entry else "GL Entry"


@dataclass(frozen=True)
class StockAccountComparisonResult:
    rows: tuple[StockAccountComparisonRow, ...]
    currency: str


def _reconciliation_key(entry, sources):
    if entry.voucher_type != "Stock Reconciliation":
        return entry.voucher_type, entry.voucher_no
    source = sources.get(entry.voucher_no)
    if source is None:
        return entry.voucher_type, entry.voucher_no
    receipt, issue, native_gl = source
    if native_gl or not (receipt and issue) or (entry.is_value_reset and entry.actual_qty >= ZERO):
        return entry.voucher_type, entry.voucher_no
    return "Stock Entry", issue if entry.actual_qty < ZERO else receipt


def stock_account_comparison(*, company, as_on_date, from_date=None, account=None):
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if not company.enable_perpetual_inventory:
        raise ValidationError("Perpetual inventory is required for this report.")
    if not isinstance(as_on_date, date) or (from_date is not None and (
        not isinstance(from_date, date) or from_date > as_on_date
    )):
        raise ValidationError("Select a valid report date range.")
    if account is not None:
        if not isinstance(account, Account):
            raise TypeError("account must be an Account instance")
        if account.company_id != company.pk or account.account_type != "Stock" or account.is_group:
            raise ValidationError("Select a leaf Stock account from the selected company.")

    warehouse_accounts = None
    if account is not None:
        warehouse_accounts = {}
        for warehouse in Warehouse.objects.filter(company=company, is_group=False).select_related(
            "company", "account", "parent_warehouse"
        ):
            effective = warehouse.effective_account(raise_error=False)
            warehouse_accounts[warehouse.pk] = effective.pk if effective else None
    sources = {
        name: (receipt, issue, native_gl)
        for name, receipt, issue, native_gl in StockReconciliation.objects.filter(
            company=company,
        ).values_list("pk", "receipt_entry_id", "issue_entry_id", "native_gl")
    }
    totals = {}
    stock_entries = StockLedgerEntry.objects.filter(
        company=company, is_cancelled=False, posting_date__lte=as_on_date,
    )
    if from_date is not None:
        stock_entries = stock_entries.filter(posting_date__gte=from_date)
    for entry in stock_entries.order_by("posting_datetime", "creation", "name"):
        if warehouse_accounts is not None and warehouse_accounts.get(entry.warehouse_id) != account.pk:
            continue
        key = _reconciliation_key(entry, sources)
        current = totals.setdefault(key, {"date": entry.posting_date, "stock": ZERO,
                                          "gl": ZERO, "has_stock_entry": False})
        current["stock"] += entry.stock_value_difference
        current["has_stock_entry"] = True
        current["date"] = min(current["date"], entry.posting_date)

    gl_entries = GLEntry.objects.filter(
        company=company, is_cancelled=False, posting_date__lte=as_on_date,
        account__account_type="Stock",
    )
    if from_date is not None:
        gl_entries = gl_entries.filter(posting_date__gte=from_date)
    if account is not None:
        gl_entries = gl_entries.filter(account=account)
    for entry in gl_entries.order_by("posting_date", "pk"):
        key = (entry.voucher_type, entry.voucher_no)
        if entry.voucher_type in ADJUSTMENT_VOUCHERS and entry.against_voucher_type and entry.against_voucher:
            key = (entry.against_voucher_type, entry.against_voucher)
        current = totals.setdefault(key, {"date": entry.posting_date, "stock": ZERO,
                                          "gl": ZERO, "has_stock_entry": False})
        current["gl"] += entry.debit - entry.credit
        current["date"] = min(current["date"], entry.posting_date)

    rows = tuple(sorted((
        StockAccountComparisonRow(key[0], key[1], values["date"],
                                  values["stock"], values["gl"], values["has_stock_entry"])
        for key, values in totals.items() if values["stock"] != values["gl"]
    ), key=lambda row: (row.posting_date, row.voucher_type, row.voucher_no)))
    return StockAccountComparisonResult(rows, company.default_currency_id)
