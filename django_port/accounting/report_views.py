import csv

from django import forms
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import render

from geo.models import Currency
from organizations.models import Company
from parties.models import Customer, Supplier
from projects.models import Project

from .closing_balance_report import closing_balance_report
from .general_ledger_report import general_ledger_report
from .models import Account, CostCenter, FinanceBook, FiscalYear, PeriodClosingVoucher
from .trial_balance_report import trial_balance_report
from .trial_balance_for_party_report import trial_balance_for_party_report
from .trial_balance_simple_report import trial_balance_simple_report
from .voucher_wise_balance_report import voucher_wise_balance_report


class GeneralLedgerFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    from_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    to_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    account = forms.ModelChoiceField(queryset=Account.objects.all(), required=False)
    cost_center = forms.ModelChoiceField(queryset=CostCenter.objects.all(), required=False)
    project = forms.ModelChoiceField(queryset=Project.objects.all(), required=False)
    finance_book = forms.ModelChoiceField(queryset=FinanceBook.objects.all(), required=False)
    party_type = forms.ChoiceField(choices=(("", "All"), ("Customer", "Customer"), ("Supplier", "Supplier")), required=False)
    party = forms.CharField(max_length=140, required=False, label="Party ID")
    voucher_no = forms.CharField(max_length=140, required=False, label="Voucher number")
    against_voucher_no = forms.CharField(max_length=140, required=False, label="Reference voucher number")
    print_in_account_currency = forms.BooleanField(required=False, label="Show account currency")
    group_by_account = forms.BooleanField(required=False, label="Group by account")
    group_by_party = forms.BooleanField(required=False, label="Group by party")
    group_by_voucher = forms.BooleanField(required=False, label="Group by voucher")
    consolidate_vouchers = forms.BooleanField(required=False, label="Consolidate voucher rows")
    include_default_book_entries = forms.BooleanField(required=False, initial=True)
    show_opening_entries = forms.BooleanField(required=False)
    disable_opening_balance_calculation = forms.BooleanField(required=False, label="Disable opening balance calculation")
    show_remarks = forms.BooleanField(required=False, label="Show remarks")

    def clean(self):
        data = super().clean()
        start, end = data.get("from_date"), data.get("to_date")
        if start and end and start > end:
            raise forms.ValidationError("From Date must not be after To Date.")
        party, party_type = data.get("party"), data.get("party_type")
        if party and not party_type:
            self.add_error("party_type", "Select a Party Type.")
        elif party and not {"Customer": Customer, "Supplier": Supplier}[party_type].objects.filter(pk=party).exists():
            self.add_error("party", "The selected Party does not exist.")
        return data


def _csv_text(value):
    """Keep names and notes from becoming spreadsheet formulas when opened in Excel."""
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value


def _csv_response(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="general-ledger.csv"'
    writer = csv.writer(response)
    header = ["Date", "Account", "Voucher Type", "Voucher Number", "Reference Type", "Reference Number",
              "Party Type", "Party", "Finance Book", "Cost Center", "Debit", "Credit", "Account Balance"]
    if report.group_by_party:
        header[12] = "Party Balance"
    elif report.group_by_voucher:
        header[12] = "Voucher Balance"
    if report.account_currency:
        header.extend(("Account Currency", "Debit in Account Currency", "Credit in Account Currency",
                       "Balance in Account Currency"))
    header.append("Project")
    if report.show_remarks:
        header.append("Remarks")
    writer.writerow(header)

    def write_summary(label, debit, credit, account_debit, account_credit, account="", party_type="", party="",
                      voucher_type="", voucher_no=""):
        cells = [label, _csv_text(account), _csv_text(voucher_type), _csv_text(voucher_no), "", "",
                 _csv_text(party_type), _csv_text(party), "", "",
                 debit, credit, ""]
        if report.account_currency:
            cells.extend((report.account_currency, account_debit, account_credit, ""))
        cells.append("")
        if report.show_remarks:
            cells.append("")
        writer.writerow(cells)

    write_summary("Opening", report.opening_debit, report.opening_credit,
                  report.opening_debit_in_account_currency, report.opening_credit_in_account_currency)
    def write_entry(row):
        entry = row.entry
        cells = [entry.posting_date, _csv_text(entry.account_id), _csv_text(entry.voucher_type),
                 _csv_text(entry.voucher_no), _csv_text(entry.against_voucher_type), _csv_text(entry.against_voucher),
                 "Customer" if entry.customer_id else "Supplier" if entry.supplier_id else "",
                 _csv_text(entry.customer_id or entry.supplier_id), _csv_text(entry.finance_book_id),
                 _csv_text(entry.cost_center_id), row.debit, row.credit,
                 row.running_balance_in_group if (report.group_by_party or report.group_by_voucher) else row.running_balance]
        if report.account_currency:
            cells.extend((entry.account_currency_id, row.debit_in_account_currency,
                          row.credit_in_account_currency, row.running_balance_in_account_currency))
        cells.append(_csv_text(entry.project_id))
        if report.show_remarks:
            cells.append(_csv_text(entry.remarks))
        writer.writerow(cells)
    if report.group_by_voucher:
        for group in report.voucher_groups:
            write_summary("Voucher group", "", "", "", "",
                          voucher_type=group.voucher_type, voucher_no=group.voucher_no)
            for row in group.rows:
                write_entry(row)
            write_summary("Period total", group.period_debit, group.period_credit, "", "",
                          voucher_type=group.voucher_type, voucher_no=group.voucher_no)
    elif report.group_by_party:
        for group in report.party_groups:
            write_summary("Party group", "", "", "", "", party_type=group.party_type, party=group.party)
            write_summary("Opening", group.opening_debit, group.opening_credit, "", "",
                          party_type=group.party_type, party=group.party)
            for row in group.rows:
                write_entry(row)
            write_summary("Period total", group.period_debit, group.period_credit, "", "",
                          party_type=group.party_type, party=group.party)
            write_summary("Closing", group.closing_debit, group.closing_credit, "", "",
                          party_type=group.party_type, party=group.party)
    elif report.group_by_account:
        for group in report.groups:
            write_summary("Account group", "", "", "", "", account=group.account.pk)
            write_summary("Opening", group.opening_debit, group.opening_credit, "", "", account=group.account.pk)
            for row in group.rows:
                write_entry(row)
            write_summary("Period total", group.period_debit, group.period_credit, "", "", account=group.account.pk)
            write_summary("Closing", group.closing_debit, group.closing_credit, "", "", account=group.account.pk)
    else:
        for row in report.rows:
            write_entry(row)
    write_summary("Period total", report.period_debit, report.period_credit,
                  report.period_debit_in_account_currency, report.period_credit_in_account_currency)
    write_summary("Closing", report.closing_debit, report.closing_credit,
                  report.closing_debit_in_account_currency, report.closing_credit_in_account_currency)
    return response


@login_required(login_url="admin:login")
@permission_required("accounting.view_glentry", raise_exception=True)
def general_ledger_view(request):
    form = GeneralLedgerFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = general_ledger_report(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _csv_response(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "accounting/general_ledger.html", {"form": form, "report": report})


class ClosingBalanceFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    voucher = forms.ModelChoiceField(
        queryset=PeriodClosingVoucher.objects.filter(status=PeriodClosingVoucher.Status.SUBMITTED),
        label="Period closing voucher",
    )
    account = forms.ModelChoiceField(queryset=Account.objects.all(), required=False)
    cost_center = forms.ModelChoiceField(queryset=CostCenter.objects.all(), required=False)
    project = forms.ModelChoiceField(queryset=Project.objects.all(), required=False)
    finance_book = forms.ModelChoiceField(queryset=FinanceBook.objects.all(), required=False)


def _closing_balance_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="account-closing-balances.csv"'
    writer = csv.writer(response)
    writer.writerow(("Closing Voucher", "Closing Date", "Account", "Account Currency", "Cost Center",
                     "Finance Book", "Project", "Closing Entry", "Debit", "Credit", "Balance"))
    for row in report.rows:
        writer.writerow((_csv_text(report.voucher.name), row.closing_date, _csv_text(row.account_id),
                         _csv_text(row.account_currency_id), _csv_text(row.cost_center_id),
                         _csv_text(row.finance_book_id), _csv_text(row.project_id),
                         "Yes" if row.is_period_closing_voucher_entry else "No", row.debit, row.credit,
                         row.debit - row.credit))
    return response


@login_required(login_url="admin:login")
@permission_required("accounting.view_accountclosingbalance", raise_exception=True)
def closing_balance_view(request):
    form = ClosingBalanceFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = closing_balance_report(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _closing_balance_csv(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "accounting/closing_balances.html", {"form": form, "report": report})


class TrialBalanceFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    fiscal_year = forms.ModelChoiceField(queryset=FiscalYear.objects.filter(disabled=False))
    from_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    to_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    cost_center = forms.ModelChoiceField(queryset=CostCenter.objects.all(), required=False)
    project = forms.ModelChoiceField(queryset=Project.objects.all(), required=False)
    finance_book = forms.ModelChoiceField(queryset=FinanceBook.objects.all(), required=False)
    presentation_currency = forms.ModelChoiceField(queryset=Currency.objects.filter(enabled=True), required=False)
    include_default_book_entries = forms.BooleanField(required=False, initial=True)
    with_period_closing_entry_for_opening = forms.BooleanField(
        required=False, initial=True, label="Include closing entries in opening balance",
    )
    with_period_closing_entry_for_current_period = forms.BooleanField(
        required=False, initial=True, label="Include closing entries in period activity",
    )
    show_unclosed_fy_pl_balances = forms.BooleanField(
        required=False, label="Show unclosed prior-year profit and loss balances",
    )
    show_zero_values = forms.BooleanField(required=False)
    show_group_accounts = forms.BooleanField(required=False, initial=True)
    show_net_values = forms.BooleanField(required=False, initial=True,
                                         label="Show net opening and closing values")


def _trial_balance_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="trial-balance.csv"'
    writer = csv.writer(response)
    writer.writerow(("Account", "Account Name", "Parent Account", "Group", "Opening Debit", "Opening Credit",
                     "Period Debit", "Period Credit", "Closing Debit", "Closing Credit", "Currency"))
    for row in report.rows:
        writer.writerow((_csv_text(row.account.pk), _csv_text(row.account.account_name),
                         _csv_text(row.account.parent_account_id), "Yes" if row.account.is_group else "No",
                         row.opening_debit, row.opening_credit, row.period_debit, row.period_credit,
                         row.closing_debit, row.closing_credit, report.currency))
    writer.writerow(("Total", "", "", "", report.total_opening_debit, report.total_opening_credit,
                     report.total_period_debit, report.total_period_credit, report.total_closing_debit,
                     report.total_closing_credit, report.currency))
    return response


@login_required(login_url="admin:login")
@permission_required("accounting.view_glentry", raise_exception=True)
def trial_balance_view(request):
    form = TrialBalanceFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = trial_balance_report(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _trial_balance_csv(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "accounting/trial_balance.html", {"form": form, "report": report})


class SimpleTrialBalanceFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())


def _trial_balance_simple_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="trial-balance-simple.csv"'
    writer = csv.writer(response)
    writer.writerow(("Fiscal Year", "Company", "Posting Date", "Account", "Debit", "Credit", "Finance Book"))
    for row in report.rows:
        writer.writerow((_csv_text(row.fiscal_year), _csv_text(row.company), row.posting_date,
                         _csv_text(row.account), row.debit, row.credit, _csv_text(row.finance_book)))
    writer.writerow(("Total", "", "", "", report.total_debit, report.total_credit, ""))
    return response


@login_required(login_url="admin:login")
@permission_required("accounting.view_glentry", raise_exception=True)
def trial_balance_simple_view(request):
    form = SimpleTrialBalanceFilterForm(request.GET or None)
    report = trial_balance_simple_report(**form.cleaned_data) if request.GET and form.is_valid() else None
    if report is not None and request.GET.get("format") == "csv":
        return _trial_balance_simple_csv(report)
    return render(request, "accounting/trial_balance_simple.html", {"form": form, "report": report})


class PartyTrialBalanceFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    fiscal_year = forms.ModelChoiceField(queryset=FiscalYear.objects.filter(disabled=False))
    from_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    to_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    party_type = forms.ChoiceField(choices=(("Customer", "Customer"), ("Supplier", "Supplier")))
    party = forms.CharField(max_length=140, required=False, label="Party ID")
    account = forms.ModelChoiceField(queryset=Account.objects.all(), required=False)
    show_zero_values = forms.BooleanField(required=False)
    exclude_zero_balance_parties = forms.BooleanField(required=False, initial=True)


def _party_trial_balance_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="trial-balance-for-party.csv"'
    writer = csv.writer(response)
    writer.writerow(("Party", "Party Name", "Opening Debit", "Opening Credit", "Period Debit", "Period Credit",
                     "Closing Debit", "Closing Credit", "Currency"))
    for row in report.rows:
        writer.writerow((_csv_text(row.party), _csv_text(row.party_name), row.opening_debit, row.opening_credit,
                         row.debit, row.credit, row.closing_debit, row.closing_credit, report.currency))
    writer.writerow(("Total", "", report.total_opening_debit, report.total_opening_credit,
                     report.total_debit, report.total_credit, report.total_closing_debit,
                     report.total_closing_credit, report.currency))
    return response


@login_required(login_url="admin:login")
@permission_required("accounting.view_glentry", raise_exception=True)
def trial_balance_for_party_view(request):
    form = PartyTrialBalanceFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = trial_balance_for_party_report(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _party_trial_balance_csv(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "accounting/trial_balance_for_party.html", {"form": form, "report": report})


class VoucherBalanceFilterForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.all())
    voucher_type = forms.CharField(max_length=140, required=False)
    from_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    to_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))


def _voucher_balance_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="voucher-wise-balance.csv"'
    writer = csv.writer(response)
    writer.writerow(("Voucher Type", "Voucher Number", "Debit", "Credit", "Difference", "Currency"))
    for row in report.rows:
        writer.writerow((_csv_text(row.voucher_type), _csv_text(row.voucher_no),
                         row.debit, row.credit, row.difference, report.currency))
    return response


@login_required(login_url="admin:login")
@permission_required("accounting.view_glentry", raise_exception=True)
def voucher_wise_balance_view(request):
    form = VoucherBalanceFilterForm(request.GET or None)
    report = None
    if request.GET and form.is_valid():
        try:
            report = voucher_wise_balance_report(**form.cleaned_data)
            if request.GET.get("format") == "csv":
                return _voucher_balance_csv(report)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "accounting/voucher_wise_balance.html", {"form": form, "report": report})
