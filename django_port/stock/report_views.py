"""Read-only stock report pages and CSV downloads."""

import csv

from django import forms
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import render

from catalog.models import Item
from organizations.models import Company
from projects.models import Project

from .models import Warehouse
from .stock_ledger_report import stock_ledger_report


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
