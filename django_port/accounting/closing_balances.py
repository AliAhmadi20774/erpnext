"""Cumulative closing snapshots for the supported GL dimensions."""

from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db.models import Sum

from geo.exchange import lookup_exchange_rate

from .models import AccountClosingBalance, GLEntry, PeriodClosingVoucher


AMOUNT_QUANTUM = Decimal("0.000000001")
AMOUNT_FIELDS = (
    "debit", "credit", "debit_in_account_currency", "credit_in_account_currency",
)


def create_closing_balances(voucher):
    """Carry forward the last snapshot and add this period's GL activity."""
    if voucher.closing_balances.exists():
        raise ValidationError("Closing balances already exist for this voucher.")
    company = voucher.company
    prior = PeriodClosingVoucher.objects.filter(
        company=company, status=PeriodClosingVoucher.Status.SUBMITTED,
        period_end_date__lt=voucher.period_end_date,
    ).order_by("-period_end_date").first()
    totals = {}

    def add(row, *, is_closing):
        key = (
            row["account_id"], row["account_currency_id"], row["cost_center_id"],
            row["finance_book_id"], row["project_id"], is_closing,
        )
        amounts = totals.setdefault(key, {field: Decimal("0") for field in AMOUNT_FIELDS})
        for field in AMOUNT_FIELDS:
            amounts[field] += row[field] or Decimal("0")

    if prior:
        for row in prior.closing_balances.values(
            "account_id", "account_currency_id", "cost_center_id", "finance_book_id", "project_id",
            "is_period_closing_voucher_entry", *AMOUNT_FIELDS,
        ):
            add(row, is_closing=row["is_period_closing_voucher_entry"])

    ordinary = GLEntry.objects.filter(
        company=company, posting_date__range=(voucher.period_start_date, voucher.period_end_date),
        is_cancelled=False, is_opening=False,
    ).exclude(voucher_type="Period Closing Voucher")
    if not prior:
        ordinary = ordinary | GLEntry.objects.filter(
            company=company, posting_date__lte=voucher.period_end_date,
            is_cancelled=False, is_opening=True, account__report_type="Balance Sheet",
        )
    for row in ordinary.values("account_id", "account_currency_id", "cost_center_id", "finance_book_id", "project_id").annotate(
        **{field: Sum(field) for field in AMOUNT_FIELDS},
    ):
        if row["debit"] != row["credit"]:
            add(row, is_closing=False)

    closing_rows = GLEntry.objects.filter(
        company=company, voucher_type="Period Closing Voucher", voucher_no=voucher.name,
        is_cancelled=False,
    )
    for row in closing_rows.values("account_id", "account_currency_id", "cost_center_id", "finance_book_id", "project_id").annotate(
        **{field: Sum(field) for field in AMOUNT_FIELDS},
    ):
        add(row, is_closing=True)

    if not totals:
        return []
    reporting_currency_id = company.reporting_currency_id or company.default_currency_id
    rate = lookup_exchange_rate(company.default_currency_id, reporting_currency_id, voucher.period_end_date)
    snapshots = []
    for (account_id, account_currency_id, cost_center_id, finance_book_id, project_id, is_closing), amounts in totals.items():
        snapshots.append(AccountClosingBalance(
            period_closing_voucher=voucher, closing_date=voucher.period_end_date,
            company=company, account_id=account_id, account_currency_id=account_currency_id,
            cost_center_id=cost_center_id, finance_book_id=finance_book_id, project_id=project_id,
            is_period_closing_voucher_entry=is_closing,
            reporting_currency_exchange_rate=rate,
            debit_in_reporting_currency=(amounts["debit"] * rate).quantize(AMOUNT_QUANTUM, rounding=ROUND_HALF_UP),
            credit_in_reporting_currency=(amounts["credit"] * rate).quantize(AMOUNT_QUANTUM, rounding=ROUND_HALF_UP),
            **amounts,
        ))
    return AccountClosingBalance.objects.bulk_create(snapshots)
