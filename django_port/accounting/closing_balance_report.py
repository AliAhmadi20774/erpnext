"""Read-only view of a submitted period closing snapshot."""

from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError

from organizations.models import Company
from projects.models import Project

from .models import Account, AccountClosingBalance, CostCenter, FinanceBook, PeriodClosingVoucher


@dataclass(frozen=True)
class ClosingBalanceResult:
    voucher: PeriodClosingVoucher
    rows: tuple[AccountClosingBalance, ...]
    ordinary_debit: Decimal
    ordinary_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal


def closing_balance_report(*, company, voucher, account=None, cost_center=None, project=None, finance_book=None):
    """Return cumulative snapshot rows, retaining ordinary and closing entries separately."""
    if not isinstance(company, Company) or not isinstance(voucher, PeriodClosingVoucher):
        raise TypeError("Select a company and a period closing voucher.")
    if voucher.company_id != company.pk or voucher.status != PeriodClosingVoucher.Status.SUBMITTED:
        raise ValidationError("Select a submitted period closing voucher for this company.")
    for value, model, label in (
        (account, Account, "Account"), (cost_center, CostCenter, "Cost center"),
        (project, Project, "Project"),
    ):
        if value is not None and (not isinstance(value, model) or value.company_id != company.pk):
            raise ValidationError(f"{label} must belong to the selected company.")
    if finance_book is not None and not isinstance(finance_book, FinanceBook):
        raise TypeError("finance_book must be a FinanceBook instance")

    rows = AccountClosingBalance.objects.filter(period_closing_voucher=voucher, company=company)
    if account is not None:
        account = Account.objects.get(pk=account.pk)
        rows = rows.filter(account__lft__gte=account.lft, account__rgt__lte=account.rgt)
    if cost_center is not None:
        cost_center = CostCenter.objects.get(pk=cost_center.pk)
        rows = rows.filter(cost_center__lft__gte=cost_center.lft, cost_center__rgt__lte=cost_center.rgt)
    if project is not None:
        rows = rows.filter(project=project)
    if finance_book is not None:
        rows = rows.filter(finance_book=finance_book)
    selected = tuple(rows.select_related("account", "cost_center", "finance_book", "project", "account_currency")
                     .order_by("account_id", "cost_center_id", "finance_book_id", "project_id",
                               "is_period_closing_voucher_entry"))
    totals = {False: [Decimal("0"), Decimal("0")], True: [Decimal("0"), Decimal("0")]}
    for row in selected:
        amounts = totals[row.is_period_closing_voucher_entry]
        amounts[0] += row.debit
        amounts[1] += row.credit
    return ClosingBalanceResult(voucher, selected, *totals[False], *totals[True])
