"""Read-only Stock Ledger report for the stock vouchers currently ported."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError

from catalog.models import Item
from organizations.models import Company
from projects.models import Project

from .models import StockLedgerEntry, Warehouse


ZERO = Decimal("0")


@dataclass(frozen=True)
class StockLedgerOpening:
    quantity: Decimal
    stock_value: Decimal
    valuation_rate: Decimal


@dataclass(frozen=True)
class StockLedgerReportRow:
    entry: StockLedgerEntry

    @property
    def in_qty(self):
        return max(self.entry.actual_qty, ZERO)

    @property
    def out_qty(self):
        return min(self.entry.actual_qty, ZERO)


@dataclass(frozen=True)
class StockLedgerReportResult:
    rows: tuple[StockLedgerReportRow, ...]
    opening: StockLedgerOpening | None
    currency: str


def stock_ledger_report(*, company, from_date, to_date, item=None, warehouse=None,
                        project=None, voucher_no=""):
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if not isinstance(from_date, date) or not isinstance(to_date, date) or from_date > to_date:
        raise ValidationError("Select a valid stock ledger date range.")
    if item is not None and not isinstance(item, Item):
        raise TypeError("item must be an Item instance")
    if warehouse is not None:
        if not isinstance(warehouse, Warehouse):
            raise TypeError("warehouse must be a Warehouse instance")
        if warehouse.company_id != company.pk:
            raise ValidationError("Warehouse must belong to the selected company.")
        warehouse = Warehouse.objects.get(pk=warehouse.pk)
    if project is not None:
        if not isinstance(project, Project):
            raise TypeError("project must be a Project instance")
        if project.company_id != company.pk:
            raise ValidationError("Project must belong to the selected company.")
    voucher_no = (voucher_no or "").strip()

    entries = StockLedgerEntry.objects.filter(company=company, is_cancelled=False)
    if item is not None:
        entries = entries.filter(item=item)
    if warehouse is not None:
        entries = entries.filter(warehouse__lft__gte=warehouse.lft,
                                 warehouse__rgt__lte=warehouse.rgt)
    if project is not None:
        entries = entries.filter(project=project)
    if voucher_no:
        entries = entries.filter(voucher_no=voucher_no)

    opening = None
    if item is not None and warehouse is not None and project is None and not voucher_no:
        latest_by_warehouse = {}
        for entry in entries.filter(posting_date__lt=from_date).order_by(
            "posting_datetime", "creation", "name"
        ):
            latest_by_warehouse[entry.warehouse_id] = entry
        quantity = sum((entry.qty_after_transaction for entry in latest_by_warehouse.values()), ZERO)
        value = sum((entry.stock_value for entry in latest_by_warehouse.values()), ZERO)
        opening = StockLedgerOpening(quantity, value, value / quantity if quantity else ZERO)

    rows = tuple(StockLedgerReportRow(entry) for entry in entries.filter(
        posting_date__gte=from_date, posting_date__lte=to_date,
    ).select_related("item", "warehouse", "stock_uom", "project").order_by(
        "posting_datetime", "creation", "name"
    ))
    return StockLedgerReportResult(rows, opening, company.default_currency_id)
