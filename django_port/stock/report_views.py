"""Read-only stock report pages and CSV downloads."""

import csv

from django import forms
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import render

from accounting.models import Account
from catalog.models import Item, ItemGroup
from organizations.models import Company
from projects.models import Project

from .models import Warehouse, WarehouseType
from .stock_account_comparison import stock_account_comparison
from .stock_balance_report import stock_balance_report
from .stock_invariant_report import stock_invariant_report
from .stock_ledger_report import stock_ledger_report
from .stock_variance_report import stock_variance_report
from .total_stock_summary import total_stock_summary
from .warehouse_balance_report import warehouse_balance_report


class StockLedgerFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    from_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    to_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    item = forms.ModelChoiceField(queryset=Item.objects.all(), required=False)
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.all(), required=False)
    project = forms.ModelChoiceField(queryset=Project.objects.all(), required=False)
    voucher_no = forms.CharField(max_length=140, required=False, label="Voucher number")

    def clean(self):
        data = super().clean()
        start, end = data.get("from_date"), data.get("to_date")
        if start and end and start > end:
            raise forms.ValidationError("From Date must not be after To Date.")
        return data


class StockBalanceFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    from_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    to_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    item = forms.ModelChoiceField(queryset=Item.objects.all(), required=False)
    item_group = forms.ModelChoiceField(queryset=ItemGroup.objects.all(), required=False)
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.all(), required=False)
    warehouse_type = forms.ModelChoiceField(queryset=WarehouseType.objects.all(), required=False)
    include_zero_stock = forms.BooleanField(required=False, label="Include zero balance rows")

    def clean(self):
        data = super().clean()
        start, end = data.get("from_date"), data.get("to_date")
        if start and end and start > end:
            raise forms.ValidationError("From Date must not be after To Date.")
        return data


class StockInvariantFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    item = forms.ModelChoiceField(queryset=Item.objects.all())
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.all())
    show_incorrect_entries = forms.BooleanField(required=False, label="Show from first incorrect entry")


class StockAccountComparisonFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    from_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    as_on_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    account = forms.ModelChoiceField(
        queryset=Account.objects.filter(account_type="Stock", is_group=False), required=False,
    )

    def clean(self):
        data = super().clean()
        start, end = data.get("from_date"), data.get("as_on_date")
        if start and end and start > end:
            raise forms.ValidationError("From Date must not be after As On Date.")
        return data


class StockVarianceFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    item = forms.ModelChoiceField(queryset=Item.objects.all(), required=False)
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.filter(is_group=False), required=False)
    difference_in = forms.ChoiceField(choices=(
        ("All", "All differences"), ("Qty", "Quantity"),
        ("Value", "Value"), ("Valuation", "Valuation rate"),
    ))
    include_disabled = forms.BooleanField(required=False, label="Include disabled items and warehouses")


class WarehouseBalanceFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    show_disabled_warehouses = forms.BooleanField(required=False, label="Show disabled warehouses")


class TotalStockSummaryFilterForm(forms.Form):
    group_by = forms.ChoiceField(choices=(("Warehouse", "Warehouse"), ("Company", "Company")))
    company = forms.ModelChoiceField(queryset=Company.objects.all(), required=False,
                                     label="Company (required for warehouse grouping)")

    def clean(self):
        data = super().clean()
        if data.get("group_by") == "Warehouse" and not data.get("company"):
            self.add_error("company", "Select a company for warehouse grouping.")
        return data


def _csv_text(value):
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value


def _stock_ledger_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="stock-ledger.csv"'
    writer = csv.writer(response)
    writer.writerow(("Date", "Item", "Stock UOM", "In Qty", "Out Qty", "Balance Qty",
                     "Warehouse", "Incoming Rate", "Outgoing Rate", "Valuation Rate",
                     "Balance Value", "Value Change", "Voucher Type", "Voucher Number", "Project"))
    if report.opening is not None:
        writer.writerow(("Opening", "", "", "", "", report.opening.quantity, "", "", "",
                         report.opening.valuation_rate, report.opening.stock_value, "", "", "", ""))
    for row in report.rows:
        entry = row.entry
        writer.writerow((entry.posting_datetime, _csv_text(entry.item_id), _csv_text(entry.stock_uom_id),
                         row.in_qty, row.out_qty, entry.qty_after_transaction,
                         _csv_text(entry.warehouse_id), entry.incoming_rate, entry.outgoing_rate,
                         entry.valuation_rate, entry.stock_value, entry.stock_value_difference,
                         _csv_text(entry.voucher_type), _csv_text(entry.voucher_no),
                         _csv_text(entry.project_id)))
    return response


def _stock_balance_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="stock-balance.csv"'
    writer = csv.writer(response)
    writer.writerow(("Item", "Item Name", "Item Group", "Warehouse", "Stock UOM",
                     "Opening Qty", "Opening Value", "In Qty", "In Value", "Out Qty",
                     "Out Value", "Balance Qty", "Balance Value", "Valuation Rate", "Company"))
    for row in report.rows:
        writer.writerow((_csv_text(row.item.pk),
                         _csv_text(row.item.item_name), _csv_text(row.item.item_group_id),
                         _csv_text(row.warehouse.pk), _csv_text(row.stock_uom),
                         row.opening_qty, row.opening_value, row.in_qty, row.in_value,
                         row.out_qty, row.out_value, row.balance_qty, row.balance_value,
                         row.valuation_rate, _csv_text(row.warehouse.company_id)))
    return response


def _stock_invariant_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="stock-ledger-invariant-check.csv"'
    writer = csv.writer(response)
    writer.writerow(("Entry", "Posting Date", "Voucher Type", "Voucher Number",
                     "Qty Change", "Qty After", "Expected Qty", "Qty Difference",
                     "Stock Value", "Expected Value", "Value Difference", "Valuation Rate",
                     "Rate Difference", "Queue Qty Difference", "Queue Value Difference",
                     "Queue Rate Difference", "Queue Error"))
    for row in report.rows:
        entry = row.entry
        writer.writerow((_csv_text(entry.pk), entry.posting_date, _csv_text(entry.voucher_type),
                         _csv_text(entry.voucher_no), entry.actual_qty,
                         entry.qty_after_transaction, row.expected_qty, row.qty_difference,
                         entry.stock_value, row.expected_value, row.value_difference,
                         entry.valuation_rate, row.rate_difference, row.queue_qty_difference,
                         row.queue_value_difference, row.queue_rate_difference,
                         row.queue_error))
    check = report.bin_check
    writer.writerow(("Bin", "", "", "", "", check.bin.actual_qty if check.bin else "",
                     check.expected_qty, check.qty_difference,
                     check.bin.stock_value if check.bin else "", check.expected_value,
                     check.value_difference, check.bin.valuation_rate if check.bin else "",
                     check.rate_difference, "", "", "", ""))
    return response


def _stock_account_comparison_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="stock-account-comparison.csv"'
    writer = csv.writer(response)
    writer.writerow(("Ledger Type", "Posting Date", "Voucher Type", "Voucher Number",
                     "Stock Value", "Account Value", "Difference Value"))
    for row in report.rows:
        writer.writerow((row.ledger_type, row.posting_date, _csv_text(row.voucher_type),
                         _csv_text(row.voucher_no), row.stock_value, row.account_value,
                         row.difference_value))
    return response


def _stock_variance_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="stock-ledger-variance.csv"'
    writer = csv.writer(response)
    writer.writerow(("Item", "Warehouse", "Valuation Method", "Source", "Entry", "Date",
                     "Qty Difference", "Value Difference", "Rate Difference",
                     "Queue Qty Difference", "Queue Value Difference",
                     "Queue Rate Difference", "Queue Error"))
    for row in report.rows:
        writer.writerow((_csv_text(row.item.pk), _csv_text(row.warehouse.pk),
                         row.valuation_method, row.source, _csv_text(row.entry_name),
                         row.posting_date or "", row.qty_difference, row.value_difference,
                         row.rate_difference, row.queue_qty_difference,
                         row.queue_value_difference, row.queue_rate_difference,
                         row.queue_error))
    return response


def _warehouse_balance_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="warehouse-wise-stock-balance.csv"'
    writer = csv.writer(response)
    writer.writerow(("Warehouse", "Parent Warehouse", "Depth", "Is Group", "Disabled", "Stock Balance"))
    for row in report.rows:
        warehouse = row.warehouse
        writer.writerow((_csv_text(warehouse.pk), _csv_text(warehouse.parent_warehouse_id),
                         row.indent, warehouse.is_group, warehouse.disabled, row.stock_balance))
    return response


def _total_stock_summary_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="total-stock-summary.csv"'
    writer = csv.writer(response)
    writer.writerow((report.group_by, "Item", "Description", "Current Qty"))
    for row in report.rows:
        writer.writerow((_csv_text(row.group_name), _csv_text(row.item_code),
                         _csv_text(row.description), row.current_qty))
    return response


@login_required(login_url="admin:login")
@permission_required("stock.view_stockledgerentry", raise_exception=True)
def stock_ledger_view(request):
    form = StockLedgerFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = stock_ledger_report(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _stock_ledger_csv(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "stock/stock_ledger.html", {"form": form, "report": report})


@login_required(login_url="admin:login")
@permission_required("stock.view_stockledgerentry", raise_exception=True)
def stock_balance_view(request):
    form = StockBalanceFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = stock_balance_report(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _stock_balance_csv(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "stock/stock_balance.html", {"form": form, "report": report})


@login_required(login_url="admin:login")
@permission_required(("stock.view_stockledgerentry", "stock.view_bin"), raise_exception=True)
def stock_invariant_view(request):
    form = StockInvariantFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = stock_invariant_report(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _stock_invariant_csv(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "stock/stock_invariant.html", {"form": form, "report": report})


@login_required(login_url="admin:login")
@permission_required(("stock.view_stockledgerentry", "accounting.view_glentry"), raise_exception=True)
def stock_account_comparison_view(request):
    form = StockAccountComparisonFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = stock_account_comparison(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _stock_account_comparison_csv(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "stock/stock_account_comparison.html", {"form": form, "report": report})


@login_required(login_url="admin:login")
@permission_required(("stock.view_stockledgerentry", "stock.view_bin"), raise_exception=True)
def stock_variance_view(request):
    form = StockVarianceFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = stock_variance_report(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _stock_variance_csv(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "stock/stock_variance.html", {"form": form, "report": report})


@login_required(login_url="admin:login")
@permission_required(("stock.view_stockledgerentry", "stock.view_warehouse"), raise_exception=True)
def warehouse_balance_view(request):
    form = WarehouseBalanceFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        report = warehouse_balance_report(**form.cleaned_data)
        if request.GET.get("format") == "csv":
            return _warehouse_balance_csv(report)
    return render(request, "stock/warehouse_balance.html", {"form": form, "report": report})


@login_required(login_url="admin:login")
@permission_required("stock.view_bin", raise_exception=True)
def total_stock_summary_view(request):
    form = TotalStockSummaryFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = total_stock_summary(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _total_stock_summary_csv(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "stock/total_stock_summary.html", {"form": form, "report": report})
