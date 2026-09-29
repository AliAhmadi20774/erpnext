"""Restricted, synchronous period closing for the currently supported ledger."""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from organizations.models import Company
from projects.models import Project

from .fiscal import resolve_fiscal_year
from .ledger import LedgerLine, post_gl_entries
from .models import Account, CostCenter, FinanceBook, GLEntry, PeriodClosingVoucher
from .periods import validate_accounting_period
from .closing_balances import create_closing_balances


@transaction.atomic
def submit_period_closing_voucher(voucher, *, user=None):
    """Reverse period P&L balances into one company-currency closing account."""
    if not isinstance(voucher, PeriodClosingVoucher) or not voucher.pk:
        raise TypeError("voucher must be a saved PeriodClosingVoucher")
    company = Company.objects.select_for_update().get(pk=voucher.company_id)
    voucher = PeriodClosingVoucher.objects.select_for_update().get(pk=voucher.pk)
    if voucher.status != PeriodClosingVoucher.Status.DRAFT:
        raise ValidationError("Period closing voucher has already been submitted.")
    voucher.full_clean()
    if resolve_fiscal_year(voucher.period_end_date, company).pk != voucher.fiscal_year_id:
        raise ValidationError("Select the applicable fiscal year for the closing date.")
    validate_accounting_period(
        company=company, posting_date=voucher.period_end_date,
        document_type="Period Closing Voucher", user=user,
    )

    previous_date = voucher.fiscal_year.year_start_date - timedelta(days=1)
    try:
        previous_year = resolve_fiscal_year(previous_date, company)
    except ValidationError:
        previous_year = None
    if previous_year and GLEntry.objects.filter(
        company=company, posting_date__range=(previous_year.year_start_date, previous_date),
        is_cancelled=False,
    ).exists() and not PeriodClosingVoucher.objects.filter(
        company=company, status=PeriodClosingVoucher.Status.SUBMITTED,
        period_end_date__range=(previous_year.year_start_date, previous_date),
    ).exists():
        raise ValidationError("Close the previous fiscal year before this one.")

    rows = GLEntry.objects.filter(
        company=company, posting_date__range=(voucher.period_start_date, voucher.period_end_date),
        is_cancelled=False, is_opening=False,
    ).exclude(voucher_type="Period Closing Voucher")
    if rows.filter(account__account_type="Stock").exists():
        raise ValidationError("Stock valuation and stock closing must be ported before closing a period with stock entries.")
    profit_rows = rows.filter(account__report_type="Profit and Loss")
    if profit_rows.exclude(account__account_currency_id=company.default_currency_id).exists():
        raise ValidationError("Foreign-currency profit-and-loss closing is not supported yet.")
    balances = list(profit_rows.values("account_id", "cost_center_id", "finance_book_id", "project_id").annotate(
        total_debit=Sum("debit"), total_credit=Sum("credit"),
    ).order_by("account_id", "cost_center_id", "finance_book_id", "project_id"))
    accounts = Account.objects.in_bulk(row["account_id"] for row in balances)
    cost_centers = CostCenter.objects.in_bulk(
        {row["cost_center_id"] for row in balances if row["cost_center_id"]}
    )
    finance_books = FinanceBook.objects.in_bulk(
        {row["finance_book_id"] for row in balances if row["finance_book_id"]}
    )
    projects = Project.objects.in_bulk(
        {row["project_id"] for row in balances if row["project_id"]}
    )
    lines = []
    by_dimension = {}
    for row in balances:
        balance = row["total_debit"] - row["total_credit"]
        if not balance:
            continue
        cost_center = cost_centers.get(row["cost_center_id"])
        finance_book = finance_books.get(row["finance_book_id"])
        project = projects.get(row["project_id"])
        lines.append(LedgerLine(
            account=accounts[row["account_id"]], cost_center=cost_center,
            finance_book=finance_book, project=project,
            debit=-balance if balance < 0 else Decimal("0"),
            credit=balance if balance > 0 else Decimal("0"),
            remarks=voucher.remarks,
        ))
        dimension_key = (row["cost_center_id"], row["finance_book_id"], row["project_id"])
        by_dimension[dimension_key] = by_dimension.get(dimension_key, Decimal("0")) + balance
    for (cost_center_id, finance_book_id, project_id), balance in by_dimension.items():
        if not balance:
            continue
        lines.append(LedgerLine(
            account=voucher.closing_account_head, cost_center=cost_centers.get(cost_center_id),
            finance_book=finance_books.get(finance_book_id), project=projects.get(project_id),
            debit=balance if balance > 0 else Decimal("0"),
            credit=-balance if balance < 0 else Decimal("0"),
            remarks=voucher.remarks,
        ))
    if lines:
        post_gl_entries(
            company=company, posting_date=voucher.period_end_date,
            voucher_type="Period Closing Voucher", voucher_no=voucher.name,
            lines=lines, user=user,
        )
    create_closing_balances(voucher)
    voucher.status = PeriodClosingVoucher.Status.SUBMITTED
    voucher._submitting = True
    voucher.save(update_fields=("status",))
    return voucher
