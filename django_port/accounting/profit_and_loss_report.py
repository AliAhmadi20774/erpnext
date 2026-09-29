"""Single-range Profit and Loss Statement from current-period Trial Balance rows."""

from dataclasses import dataclass
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError

from .models import Account, FiscalYear
from .trial_balance_report import trial_balance_report


ZERO = Decimal("0")


@dataclass(frozen=True)
class ProfitAndLossAccountRow:
    account: Account
    amount: Decimal


@dataclass(frozen=True)
class ProfitAndLossResult:
    income: tuple[ProfitAndLossAccountRow, ...]
    expense: tuple[ProfitAndLossAccountRow, ...]
    total_income: Decimal
    total_expense: Decimal
    net_profit_loss: Decimal
    currency: str


@dataclass(frozen=True)
class ProfitAndLossComparisonRow:
    section: str
    label: str
    amounts: tuple[Decimal, ...]
    total: Decimal


@dataclass(frozen=True)
class ProfitAndLossComparisonResult:
    labels: tuple[str, ...]
    rows: tuple[ProfitAndLossComparisonRow, ...]
    currency: str
    accumulated_values: bool


def profit_and_loss_report(*, company, fiscal_year, from_date, to_date,
                           cost_center=None, project=None, finance_book=None,
                           include_default_book_entries=True, presentation_currency=None,
                           show_zero_values=False):
    """Return income, expense, and net profit for one fiscal-year date range."""
    if not isinstance(fiscal_year, FiscalYear):
        raise TypeError("Select a fiscal year.")
    trial = trial_balance_report(
        company=company, fiscal_year=fiscal_year, from_date=from_date, to_date=to_date,
        cost_center=cost_center, project=project, finance_book=finance_book,
        include_default_book_entries=include_default_book_entries,
        presentation_currency=presentation_currency, show_zero_values=show_zero_values,
        show_group_accounts=True, show_net_values=False,
        with_period_closing_entry_for_current_period=False,
    )
    sections = {"Income": [], "Expense": []}
    totals = {"Income": ZERO, "Expense": ZERO}
    for row in trial.rows:
        root = row.account.root_type
        if root not in sections:
            continue
        amount = (row.period_credit - row.period_debit if root == "Income"
                  else row.period_debit - row.period_credit)
        if show_zero_values or amount:
            sections[root].append(ProfitAndLossAccountRow(row.account, amount))
        if row.account.parent_account_id is None:
            totals[root] += amount
    return ProfitAndLossResult(
        income=tuple(sections["Income"]), expense=tuple(sections["Expense"]),
        total_income=totals["Income"], total_expense=totals["Expense"],
        net_profit_loss=totals["Income"] - totals["Expense"], currency=trial.currency,
    )


def _add_months(day, count):
    year, month_index = divmod(day.year * 12 + day.month - 1 + count, 12)
    month = month_index + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def profit_and_loss_comparison_report(*, company, fiscal_year, from_date, to_date,
                                      periodicity="Monthly", accumulated_values=False,
                                      cost_center=None, project=None, finance_book=None,
                                      include_default_book_entries=True,
                                      presentation_currency=None, show_zero_values=False):
    """Compare period activity or running totals within one fiscal year."""
    if not isinstance(fiscal_year, FiscalYear):
        raise TypeError("Select a fiscal year.")
    if (not isinstance(from_date, date) or not isinstance(to_date, date)
            or not fiscal_year.year_start_date <= from_date <= to_date <= fiscal_year.year_end_date):
        raise ValidationError("Select dates inside the fiscal year, with From Date before To Date.")
    months = {"Monthly": 1, "Quarterly": 3, "Half-Yearly": 6, "Yearly": 12}.get(periodicity)
    if months is None:
        raise ValidationError("Select a valid periodicity.")
    ends = []
    index = months
    while True:
        end = min(_add_months(fiscal_year.year_start_date, index) - timedelta(days=1), to_date)
        if end >= from_date:
            ends.append(end)
        if end >= to_date:
            break
        index += months
    starts = [from_date, *(day + timedelta(days=1) for day in ends[:-1])]
    options = dict(company=company, fiscal_year=fiscal_year, cost_center=cost_center,
                   project=project, finance_book=finance_book,
                   include_default_book_entries=include_default_book_entries,
                   presentation_currency=presentation_currency, show_zero_values=show_zero_values)
    reports = [profit_and_loss_report(**options, from_date=from_date if accumulated_values else start,
                                      to_date=end) for start, end in zip(starts, ends)]

    def comparison_row(section, label, amounts):
        return ProfitAndLossComparisonRow(section, label, amounts,
                                          amounts[-1] if accumulated_values else sum(amounts, ZERO))

    rows = []
    for section, attribute, total_label, total_attribute in (
        ("Income", "income", "Total Income", "total_income"),
        ("Expense", "expense", "Total Expense", "total_expense"),
    ):
        accounts = {row.account.pk: row.account for report in reports for row in getattr(report, attribute)}
        for account in sorted(accounts.values(), key=lambda value: (value.lft, value.pk)):
            amounts = tuple(next((row.amount for row in getattr(report, attribute)
                                  if row.account.pk == account.pk), ZERO) for report in reports)
            rows.append(comparison_row(section, account.name, amounts))
        rows.append(comparison_row(section, total_label,
                                   tuple(getattr(report, total_attribute) for report in reports)))
    rows.append(comparison_row("Summary", "Net Profit/Loss",
                               tuple(report.net_profit_loss for report in reports)))
    labels = tuple(f"{start.isoformat()} to {end.isoformat()}" if not accumulated_values else end.isoformat()
                   for start, end in zip(starts, ends))
    return ProfitAndLossComparisonResult(labels, tuple(rows), reports[-1].currency, accumulated_values)
