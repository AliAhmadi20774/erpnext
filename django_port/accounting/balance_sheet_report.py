"""Single-date Balance Sheet foundation built from the Trial Balance."""

from dataclasses import dataclass
from datetime import date, timedelta
from calendar import monthrange
from decimal import Decimal

from django.core.exceptions import ValidationError
from organizations.models import Company

from .fiscal import resolve_fiscal_year
from .models import Account, FiscalYear, PeriodClosingVoucher
from .trial_balance_report import trial_balance_report


ZERO = Decimal("0")


@dataclass(frozen=True)
class BalanceSheetAccountRow:
    account: Account
    opening_amount: Decimal
    amount: Decimal


@dataclass(frozen=True)
class BalanceSheetResult:
    assets: tuple[BalanceSheetAccountRow, ...]
    liabilities: tuple[BalanceSheetAccountRow, ...]
    equity: tuple[BalanceSheetAccountRow, ...]
    total_assets: Decimal
    total_liabilities: Decimal
    total_equity: Decimal
    unclosed_prior_profit_loss: Decimal
    provisional_profit_loss: Decimal
    total_credit: Decimal
    currency: str
    snapshot_voucher: PeriodClosingVoucher | None
    presentation_rate_date: date | None


@dataclass(frozen=True)
class BalanceSheetComparisonRow:
    section: str
    label: str
    amounts: tuple[Decimal, ...]


@dataclass(frozen=True)
class BalanceSheetComparisonResult:
    dates: tuple[date, ...]
    labels: tuple[str, ...]
    rows: tuple[BalanceSheetComparisonRow, ...]
    currency: str
    selected_view: str
    accumulated_values: bool


def balance_sheet_report(*, company, fiscal_year, as_of_date, cost_center=None, project=None,
                         finance_book=None, include_default_book_entries=True,
                         presentation_currency=None, show_zero_values=False):
    """Show assets, liabilities, equity, and balancing profit/loss at one date."""
    if not isinstance(company, Company) or not isinstance(fiscal_year, FiscalYear):
        raise TypeError("Select a company and fiscal year.")
    trial = trial_balance_report(
        company=company, fiscal_year=fiscal_year, from_date=fiscal_year.year_start_date,
        to_date=as_of_date, cost_center=cost_center, project=project,
        finance_book=finance_book, include_default_book_entries=include_default_book_entries,
        presentation_currency=presentation_currency,
        show_zero_values=show_zero_values, show_group_accounts=True,
        show_net_values=True,
    )
    sections = {"Asset": [], "Liability": [], "Equity": []}
    opening_totals = {root: ZERO for root in sections}
    closing_totals = {root: ZERO for root in sections}
    for row in trial.rows:
        root = row.account.root_type
        if root not in sections:
            continue
        if root == "Asset":
            opening_amount = row.opening_debit - row.opening_credit
            amount = row.closing_debit - row.closing_credit
        else:
            opening_amount = row.opening_credit - row.opening_debit
            amount = row.closing_credit - row.closing_debit
        if show_zero_values or opening_amount or amount:
            sections[root].append(BalanceSheetAccountRow(row.account, opening_amount, amount))
        if row.account.parent_account_id is None:
            opening_totals[root] += opening_amount
            closing_totals[root] += amount

    unclosed = opening_totals["Asset"] - opening_totals["Liability"] - opening_totals["Equity"]
    current_gap = closing_totals["Asset"] - closing_totals["Liability"] - closing_totals["Equity"]
    provisional = current_gap - unclosed
    credit_total = closing_totals["Liability"] + closing_totals["Equity"] + unclosed + provisional
    return BalanceSheetResult(
        assets=tuple(sections["Asset"]), liabilities=tuple(sections["Liability"]),
        equity=tuple(sections["Equity"]), total_assets=closing_totals["Asset"],
        total_liabilities=closing_totals["Liability"], total_equity=closing_totals["Equity"],
        unclosed_prior_profit_loss=unclosed, provisional_profit_loss=provisional,
        total_credit=credit_total, currency=trial.currency,
        snapshot_voucher=trial.snapshot_voucher, presentation_rate_date=trial.presentation_rate_date,
    )


def _add_months(day, count):
    month_index = day.year * 12 + day.month - 1 + count
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _growth_rows(rows):
    growth_rows = []
    for row in rows:
        values = [row.amounts[0]]
        for previous, current in zip(row.amounts, row.amounts[1:]):
            if previous == 0 and current > 0:
                growth = Decimal("100")
            elif previous > 0:
                growth = ((current - previous) / previous * 100).quantize(Decimal("0.01"))
            else:
                growth = ZERO
            values.append(growth)
        growth_rows.append(BalanceSheetComparisonRow(row.section, row.label, tuple(values)))
    return tuple(growth_rows)


def balance_sheet_comparison_report(*, company, fiscal_year, from_date, to_date,
                                    periodicity="Monthly", cost_center=None, project=None,
                                    finance_book=None, include_default_book_entries=True,
                                    presentation_currency=None, show_zero_values=False,
                                    selected_view="Report", accumulated_values=True):
    """Compare Balance Sheet snapshots or period activity within one fiscal year."""
    if not isinstance(fiscal_year, FiscalYear):
        raise TypeError("Select a fiscal year.")
    if (not isinstance(from_date, date) or not isinstance(to_date, date)
            or not fiscal_year.year_start_date <= from_date <= to_date <= fiscal_year.year_end_date):
        raise ValidationError("Select dates inside the fiscal year, with From Date before To Date.")
    months = {"Monthly": 1, "Quarterly": 3, "Half-Yearly": 6, "Yearly": 12}.get(periodicity)
    if months is None:
        raise ValidationError("Select a valid periodicity.")
    if selected_view not in ("Report", "Growth"):
        raise ValidationError("Select Report or Growth view.")
    dates = []
    index = months
    while True:
        period_end = min(_add_months(fiscal_year.year_start_date, index) - timedelta(days=1), to_date)
        if period_end >= from_date:
            dates.append(period_end)
        if period_end >= to_date:
            break
        index += months

    options = dict(company=company, fiscal_year=fiscal_year, cost_center=cost_center,
                   project=project, finance_book=finance_book,
                   include_default_book_entries=include_default_book_entries,
                   presentation_currency=presentation_currency, show_zero_values=show_zero_values)
    reports = [balance_sheet_report(**options, as_of_date=day) for day in dates]
    baseline = (balance_sheet_report(**options, as_of_date=from_date - timedelta(days=1))
                if not accumulated_values and from_date > fiscal_year.year_start_date else None)
    rows = []
    period_totals = {}
    for section, attribute, total_label, total_attribute in (
        ("Asset", "assets", "Total Assets", "total_assets"),
        ("Liability", "liabilities", "Total Liabilities", "total_liabilities"),
        ("Equity", "equity", "Total Equity", "total_equity"),
    ):
        accounts = {}
        for report in reports:
            for row in getattr(report, attribute):
                accounts[row.account.pk] = row.account
        if baseline:
            for row in getattr(baseline, attribute):
                accounts[row.account.pk] = row.account
        for account in sorted(accounts.values(), key=lambda account: (account.lft, account.pk)):
            report_rows = [next((row for row in getattr(report, attribute)
                                 if row.account.pk == account.pk), None) for report in reports]
            amounts = tuple(row.amount if row else ZERO for row in report_rows)
            if not accumulated_values:
                if baseline:
                    prior_row = next((row for row in getattr(baseline, attribute)
                                      if row.account.pk == account.pk), None)
                    prior = prior_row.amount if prior_row else ZERO
                else:
                    prior = report_rows[0].opening_amount if report_rows[0] else ZERO
                amounts = tuple(current - previous for previous, current in
                                zip((prior, *amounts[:-1]), amounts))
            rows.append(BalanceSheetComparisonRow(section, account.name, amounts))
        totals = tuple(getattr(report, total_attribute) for report in reports)
        if not accumulated_values:
            if baseline:
                prior = getattr(baseline, total_attribute)
            else:
                prior = sum((row.opening_amount for row in getattr(reports[0], attribute)
                             if row.account.parent_account_id is None), ZERO)
            totals = tuple(current - previous for previous, current in zip((prior, *totals[:-1]), totals))
        period_totals[section] = totals
        rows.append(BalanceSheetComparisonRow(section, total_label, totals))
    unclosed = tuple(report.unclosed_prior_profit_loss for report in reports)
    if accumulated_values:
        provisional = tuple(report.provisional_profit_loss for report in reports)
        credit = tuple(report.total_credit for report in reports)
    else:
        provisional = tuple(asset - liability - equity - opening for asset, liability, equity, opening in
                            zip(period_totals["Asset"], period_totals["Liability"], period_totals["Equity"], unclosed))
        credit = period_totals["Asset"]
    rows.extend((
        BalanceSheetComparisonRow("Summary", "Unclosed Prior Profit/Loss", unclosed),
        BalanceSheetComparisonRow("Summary", "Provisional Profit/Loss", provisional),
        BalanceSheetComparisonRow("Summary", "Total Liabilities and Equity", credit),
    ))
    if selected_view == "Growth":
        rows = _growth_rows(rows)
    labels = []
    for index, day in enumerate(dates):
        start = from_date if index == 0 else dates[index - 1] + timedelta(days=1)
        label = day.isoformat() if accumulated_values else f"{start.isoformat()} to {day.isoformat()}"
        if selected_view == "Growth" and index:
            label += " Growth %"
        labels.append(label)
    return BalanceSheetComparisonResult(tuple(dates), tuple(labels), tuple(rows), reports[-1].currency,
                                        selected_view, accumulated_values)


def balance_sheet_yearly_report(*, company, from_fiscal_year, to_fiscal_year,
                                cost_center=None, project=None, finance_book=None,
                                include_default_book_entries=True, presentation_currency=None,
                                show_zero_values=False, selected_view="Report"):
    """Compare accumulated Balance Sheets across consecutive company fiscal years."""
    if not isinstance(company, Company) or not all(isinstance(year, FiscalYear)
                                                   for year in (from_fiscal_year, to_fiscal_year)):
        raise TypeError("Select a company and fiscal years.")
    if selected_view not in ("Report", "Growth"):
        raise ValidationError("Select Report or Growth view.")
    if from_fiscal_year.year_start_date > to_fiscal_year.year_start_date:
        raise ValidationError("From fiscal year must not follow To fiscal year.")
    years = []
    cursor = from_fiscal_year.year_start_date
    while cursor <= to_fiscal_year.year_end_date:
        year = resolve_fiscal_year(cursor, company)
        if year.year_start_date != cursor or year.year_end_date > to_fiscal_year.year_end_date:
            raise ValidationError("Select consecutive fiscal years for this company.")
        years.append(year)
        cursor = year.year_end_date + timedelta(days=1)
    if years[0].pk != from_fiscal_year.pk or years[-1].pk != to_fiscal_year.pk:
        raise ValidationError("Select fiscal years applicable to this company.")

    options = dict(company=company, cost_center=cost_center, project=project,
                   finance_book=finance_book, include_default_book_entries=include_default_book_entries,
                   presentation_currency=presentation_currency, show_zero_values=show_zero_values)
    reports = [balance_sheet_comparison_report(
        **options, fiscal_year=year, from_date=year.year_start_date,
        to_date=year.year_end_date, periodicity="Yearly",
    ) for year in years]
    keys = []
    for section, total_label in (("Asset", "Total Assets"),
                                 ("Liability", "Total Liabilities"),
                                 ("Equity", "Total Equity")):
        keys.extend(dict.fromkeys((row.section, row.label) for report in reports
                                  for row in report.rows if row.section == section and row.label != total_label))
        keys.append((section, total_label))
    keys.extend(("Summary", label) for label in
                ("Unclosed Prior Profit/Loss", "Provisional Profit/Loss", "Total Liabilities and Equity"))
    lookups = [{(row.section, row.label): row.amounts[0] for row in report.rows} for report in reports]
    rows = tuple(BalanceSheetComparisonRow(section, label,
                                           tuple(lookup.get((section, label), ZERO) for lookup in lookups))
                 for section, label in keys)
    if selected_view == "Growth":
        rows = _growth_rows(rows)
    labels = tuple(f"{year.year} ({year.year_end_date.isoformat()})" +
                   (" Growth %" if selected_view == "Growth" and index else "")
                   for index, year in enumerate(years))
    return BalanceSheetComparisonResult(tuple(year.year_end_date for year in years), labels,
                                        rows, reports[-1].currency, selected_view, True)
