"""The source Trial Balance (Simple) query over uncancelled GL entries."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Max, Sum

from organizations.models import Company

from .models import GLEntry


@dataclass(frozen=True)
class SimpleTrialBalanceRow:
    fiscal_year: str
    company: str
    posting_date: date
    account: str
    debit: Decimal
    credit: Decimal
    finance_book: str | None


@dataclass(frozen=True)
class SimpleTrialBalanceResult:
    rows: tuple[SimpleTrialBalanceRow, ...]
    total_debit: Decimal
    total_credit: Decimal
    currency: str


def trial_balance_simple_report(*, company):
    """Aggregate GL by fiscal year, date, and account, including closing rows."""
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    company = Company.objects.get(pk=company.pk)
    grouped = GLEntry.objects.filter(company=company, is_cancelled=False).values(
        "fiscal_year_id", "posting_date", "account_id",
    ).annotate(
        debit_total=Sum("debit"), credit_total=Sum("credit"), finance_book_name=Max("finance_book_id"),
    ).order_by("fiscal_year_id", "posting_date", "account_id")
    rows = []
    total_debit = total_credit = Decimal("0")
    for item in grouped:
        debit, credit = item["debit_total"], item["credit_total"]
        rows.append(SimpleTrialBalanceRow(
            fiscal_year=item["fiscal_year_id"], company=company.pk,
            posting_date=item["posting_date"], account=item["account_id"],
            debit=debit, credit=credit, finance_book=item["finance_book_name"],
        ))
        total_debit += debit
        total_credit += credit
    return SimpleTrialBalanceResult(tuple(rows), total_debit, total_credit, company.default_currency_id)
