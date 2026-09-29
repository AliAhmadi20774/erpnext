"""Single-range Profit and Loss Statement from current-period Trial Balance rows."""

from dataclasses import dataclass
from decimal import Decimal

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
