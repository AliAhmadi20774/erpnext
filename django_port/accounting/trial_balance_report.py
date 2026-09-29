"""Basic Trial Balance using the last closing snapshot for opening balances."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db.models import Q, Sum

from geo.exchange import lookup_exchange_rate_as_of
from geo.models import Currency
from organizations.models import Company
from projects.models import Project

from .models import Account, AccountClosingBalance, CostCenter, FinanceBook, FiscalYear, GLEntry, PeriodClosingVoucher


ZERO = Decimal("0")
QUANTUM = Decimal("0.000000001")


@dataclass(frozen=True)
class TrialBalanceRow:
    account: Account
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal


@dataclass(frozen=True)
class TrialBalanceResult:
    rows: tuple[TrialBalanceRow, ...]
    total_opening_debit: Decimal
    total_opening_credit: Decimal
    total_period_debit: Decimal
    total_period_credit: Decimal
    total_closing_debit: Decimal
    total_closing_credit: Decimal
    currency: str
    snapshot_voucher: PeriodClosingVoucher | None
    presentation_rate_date: date | None


def _net(amount):
    return max(amount, ZERO), max(-amount, ZERO)


def _period_amounts(queryset):
    return {
        row["account_id"]: (row["debit"] or ZERO, row["credit"] or ZERO)
        for row in queryset.values("account_id").annotate(debit=Sum("debit"), credit=Sum("credit"))
    }


def trial_balance_report(*, company, fiscal_year, from_date, to_date, cost_center=None, project=None,
                         finance_book=None, presentation_currency=None, include_default_book_entries=True,
                         with_period_closing_entry_for_opening=True,
                         with_period_closing_entry_for_current_period=True,
                         show_unclosed_fy_pl_balances=False, show_zero_values=False, show_group_accounts=True,
                         show_net_values=True):
    """Return account balances for one fiscal-year date range."""
    if not isinstance(company, Company) or not isinstance(fiscal_year, FiscalYear):
        raise TypeError("Select a company and fiscal year.")
    company = Company.objects.get(pk=company.pk)
    if fiscal_year.disabled or (not fiscal_year.all_companies and
                                not fiscal_year.company_links.filter(company=company).exists()):
        raise ValidationError("Fiscal year does not apply to this company.")
    if (not isinstance(from_date, date) or not isinstance(to_date, date)
            or not fiscal_year.year_start_date <= from_date <= to_date <= fiscal_year.year_end_date):
        raise ValidationError("Select dates inside the fiscal year, with From Date before To Date.")
    for value, model, label in ((cost_center, CostCenter, "Cost center"), (project, Project, "Project")):
        if value is not None and (not isinstance(value, model) or value.company_id != company.pk):
            raise ValidationError(f"{label} must belong to the selected company.")
    if finance_book is not None and not isinstance(finance_book, FinanceBook):
        raise TypeError("finance_book must be a FinanceBook instance")
    if presentation_currency is not None:
        if not isinstance(presentation_currency, Currency) or not Currency.objects.filter(
            pk=presentation_currency.pk, enabled=True,
        ).exists():
            raise ValidationError("Select an enabled presentation currency.")
    currency_id = presentation_currency.pk if presentation_currency else company.default_currency_id
    rate, rate_date = lookup_exchange_rate_as_of(company.default_currency_id, currency_id, to_date)

    def convert(amount):
        return (amount * rate).quantize(QUANTUM, rounding=ROUND_HALF_UP)
    if include_default_book_entries and finance_book and company.default_finance_book_id:
        if finance_book.pk != company.default_finance_book_id:
            raise ValidationError("Uncheck Include Default Book Entries to select a different finance book.")

    selected_book_id = (finance_book.pk if finance_book else company.default_finance_book_id) if include_default_book_entries else (
        finance_book.pk if finance_book else None
    )

    def dimensions(rows):
        if selected_book_id:
            rows = rows.filter(Q(finance_book_id=selected_book_id) | Q(finance_book__isnull=True))
        else:
            rows = rows.filter(finance_book__isnull=True)
        if cost_center:
            center = CostCenter.objects.get(pk=cost_center.pk)
            rows = rows.filter(cost_center__lft__gte=center.lft, cost_center__rgt__lte=center.rgt,
                               cost_center__company=company)
        if project:
            rows = rows.filter(project=project)
        return rows

    snapshot = PeriodClosingVoucher.objects.filter(
        company=company, status=PeriodClosingVoucher.Status.SUBMITTED,
        period_end_date__lt=from_date,
    ).order_by("-period_end_date").first()
    opening = {}
    if snapshot:
        prior = dimensions(AccountClosingBalance.objects.filter(period_closing_voucher=snapshot, company=company))
        if not with_period_closing_entry_for_opening:
            prior = prior.filter(is_period_closing_voucher_entry=False)
        if snapshot.period_end_date < fiscal_year.year_start_date and not show_unclosed_fy_pl_balances:
            prior = prior.filter(account__report_type="Balance Sheet")
        opening = {account_id: [debit, credit] for account_id, (debit, credit) in _period_amounts(prior).items()}

    gl = dimensions(GLEntry.objects.filter(company=company, is_cancelled=False, posting_date__lte=to_date))
    if snapshot:
        older = gl.filter(posting_date__gt=snapshot.period_end_date, posting_date__lt=from_date)
        if snapshot.period_end_date < fiscal_year.year_start_date and not show_unclosed_fy_pl_balances:
            older = older.filter(
                Q(account__report_type="Balance Sheet") | Q(posting_date__gte=fiscal_year.year_start_date)
            )
    else:
        older = gl.filter(posting_date__lt=from_date)
        if not show_unclosed_fy_pl_balances:
            older = older.filter(
                Q(account__report_type="Balance Sheet") | Q(posting_date__gte=fiscal_year.year_start_date)
            )
    if not with_period_closing_entry_for_opening:
        older = older.exclude(voucher_type="Period Closing Voucher")
    opening_entries = gl.filter(is_opening=True, posting_date__gte=from_date)
    if not with_period_closing_entry_for_opening:
        opening_entries = opening_entries.exclude(voucher_type="Period Closing Voucher")
    for source in (older, opening_entries):
        for account_id, (debit, credit) in _period_amounts(source).items():
            amounts = opening.setdefault(account_id, [ZERO, ZERO])
            amounts[0] += debit
            amounts[1] += credit

    current = gl.filter(posting_date__range=(from_date, to_date), is_opening=False)
    if not with_period_closing_entry_for_current_period:
        current = current.exclude(voucher_type="Period Closing Voucher")
    period = _period_amounts(current)

    accounts = list(Account.objects.filter(company=company).order_by("lft"))
    amounts = {account.pk: [*(convert(value) for value in opening.get(account.pk, (ZERO, ZERO))),
                            *(convert(value) for value in period.get(account.pk, (ZERO, ZERO)))]
               for account in accounts}
    for account in reversed(accounts):
        if account.parent_account_id:
            parent = amounts[account.parent_account_id]
            child = amounts[account.pk]
            for index in range(4):
                parent[index] += child[index]

    rows = []
    for account in accounts:
        if account.is_group and not show_group_accounts:
            continue
        opening_dr, opening_cr, debit, credit = amounts[account.pk]
        closing_dr, closing_cr = opening_dr + debit, opening_cr + credit
        if show_net_values:
            opening_dr, opening_cr = _net(opening_dr - opening_cr)
            closing_dr, closing_cr = _net(closing_dr - closing_cr)
        if not show_zero_values and not any((opening_dr, opening_cr, debit, credit, closing_dr, closing_cr)):
            continue
        rows.append(TrialBalanceRow(account, opening_dr, opening_cr, debit, credit, closing_dr, closing_cr))

    total_rows = (row for row in rows if (not show_group_accounts or row.account.parent_account_id is None))
    totals = [ZERO] * 6
    for row in total_rows:
        for index, value in enumerate((row.opening_debit, row.opening_credit, row.period_debit,
                                       row.period_credit, row.closing_debit, row.closing_credit)):
            totals[index] += value
    return TrialBalanceResult(tuple(rows), *totals, currency_id, snapshot, rate_date)
