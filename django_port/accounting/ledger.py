"""Restricted same-currency GL posting until document and FX workflows are ported."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from organizations.models import Company
from geo.exchange import lookup_exchange_rate

from .models import Account, GLEntry, PeriodClosingVoucher
from .fiscal import resolve_fiscal_year
from .periods import validate_accounting_period


@dataclass(frozen=True)
class LedgerLine:
    account: Account
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    customer: object = None
    supplier: object = None
    cost_center: object = None
    project: object = None
    finance_book: object = None
    against_voucher_type: str = ""
    against_voucher: str = ""
    debit_in_account_currency: Decimal | None = None
    credit_in_account_currency: Decimal | None = None
    exchange_rate: Decimal | None = None
    rate_purpose: str | None = None
    remarks: str = ""


@transaction.atomic
def post_gl_entries(*, company, posting_date, voucher_type, voucher_no, lines, is_opening=False, user=None):
    """Post a balanced voucher's GL lines atomically; caller owns voucher authorization."""
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if not isinstance(posting_date, date):
        raise ValidationError("Posting date is required.")
    voucher_type = voucher_type.strip() if isinstance(voucher_type, str) else ""
    voucher_no = voucher_no.strip() if isinstance(voucher_no, str) else ""
    if not voucher_type or not voucher_no:
        raise ValidationError("Voucher type and number are required.")
    lines = list(lines)
    if len(lines) < 2:
        raise ValidationError("A voucher requires at least two GL entries.")
    company = Company.objects.select_for_update().get(pk=company.pk)
    fiscal_year = resolve_fiscal_year(posting_date, company)
    validate_accounting_period(company=company, posting_date=posting_date, document_type=voucher_type, user=user)
    if PeriodClosingVoucher.objects.filter(
        company=company, status=PeriodClosingVoucher.Status.SUBMITTED,
        period_end_date__gte=posting_date,
    ).exists():
        raise ValidationError("Cannot post on or before a submitted period closing voucher.")
    if GLEntry.objects.filter(company=company, voucher_type=voucher_type, voucher_no=voucher_no).exists():
        raise ValidationError("This voucher already has GL entries.")

    entries = []
    for line in lines:
        if not isinstance(line, LedgerLine):
            raise TypeError("Each line must be a LedgerLine.")
        reference_type = line.against_voucher_type.strip() if isinstance(line.against_voucher_type, str) else ""
        reference_name = line.against_voucher.strip() if isinstance(line.against_voucher, str) else ""
        if bool(reference_type) != bool(reference_name):
            raise ValidationError("Reference type and name must be entered together.")
        debit = Decimal(str(line.debit))
        credit = Decimal(str(line.credit))
        if line.account.account_currency_id == company.default_currency_id:
            account_debit = debit if line.debit_in_account_currency is None else Decimal(str(line.debit_in_account_currency))
            account_credit = credit if line.credit_in_account_currency is None else Decimal(str(line.credit_in_account_currency))
            rate = Decimal("1") if line.exchange_rate is None else Decimal(str(line.exchange_rate))
        else:
            if debit or credit:
                raise ValidationError("For a foreign-currency account, enter amounts in account currency only.")
            account_debit = Decimal(str(line.debit_in_account_currency or 0))
            account_credit = Decimal(str(line.credit_in_account_currency or 0))
            rate = (
                Decimal(str(line.exchange_rate)) if line.exchange_rate is not None
                else lookup_exchange_rate(line.account.account_currency_id, company.default_currency_id, posting_date, purpose=line.rate_purpose)
            )
            quantum = Decimal("0.000000001")
            debit = (account_debit * rate).quantize(quantum, rounding=ROUND_HALF_UP)
            credit = (account_credit * rate).quantize(quantum, rounding=ROUND_HALF_UP)
        entry = GLEntry(
            company=company, account=line.account, posting_date=posting_date,
            fiscal_year=fiscal_year,
            voucher_type=voucher_type, voucher_no=voucher_no,
            account_currency_id=line.account.account_currency_id,
            debit=debit, credit=credit,
            debit_in_account_currency=account_debit, credit_in_account_currency=account_credit,
            account_exchange_rate=rate,
            customer=line.customer, supplier=line.supplier, remarks=line.remarks,
            against_voucher_type=reference_type, against_voucher=reference_name,
            cost_center=line.cost_center,
            project=line.project,
            finance_book=line.finance_book,
            is_opening=is_opening,
        )
        entry.full_clean()
        entries.append(entry)
    if sum((entry.debit for entry in entries), Decimal("0")) != sum((entry.credit for entry in entries), Decimal("0")):
        raise ValidationError("Voucher debit and credit totals must match exactly.")
    return GLEntry.objects.bulk_create(entries)


def account_balance(account, *, as_of=None):
    """Debit minus credit in company currency for one account, excluding cancelled rows."""
    rows = GLEntry.objects.filter(account=account, is_cancelled=False)
    if as_of is not None:
        rows = rows.filter(posting_date__lte=as_of)
    totals = rows.aggregate(total_debit=Sum("debit"), total_credit=Sum("credit"))
    return (totals["total_debit"] or Decimal("0")) - (totals["total_credit"] or Decimal("0"))
