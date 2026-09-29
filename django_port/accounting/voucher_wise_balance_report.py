"""Find imported or legacy vouchers whose uncancelled GL rows do not balance."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum

from organizations.models import Company

from .models import GLEntry


ZERO = Decimal("0")


@dataclass(frozen=True)
class VoucherBalanceRow:
    voucher_type: str
    voucher_no: str
    debit: Decimal
    credit: Decimal

    @property
    def difference(self):
        return self.debit - self.credit


@dataclass(frozen=True)
class VoucherBalanceResult:
    rows: tuple[VoucherBalanceRow, ...]
    currency: str


def voucher_wise_balance_report(*, company, voucher_type="", from_date=None, to_date=None):
    """Group by voucher type and number; return only unbalanced groups."""
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    company = Company.objects.get(pk=company.pk)
    if not isinstance(voucher_type, str):
        raise TypeError("voucher_type must be a string")
    voucher_type = voucher_type.strip()
    if ((from_date is not None and not isinstance(from_date, date))
            or (to_date is not None and not isinstance(to_date, date))
            or (from_date is not None and to_date is not None and from_date > to_date)):
        raise ValidationError("Enter a valid From Date and To Date range.")

    entries = GLEntry.objects.filter(company=company, is_cancelled=False)
    if voucher_type:
        entries = entries.filter(voucher_type=voucher_type)
    if from_date:
        entries = entries.filter(posting_date__gte=from_date)
    if to_date:
        entries = entries.filter(posting_date__lte=to_date)
    grouped = entries.values("voucher_type", "voucher_no").annotate(
        total_debit=Sum("debit"), total_credit=Sum("credit"),
    ).order_by("voucher_type", "voucher_no")
    rows = tuple(VoucherBalanceRow(item["voucher_type"], item["voucher_no"],
                                   item["total_debit"] or ZERO, item["total_credit"] or ZERO)
                 for item in grouped if item["total_debit"] != item["total_credit"])
    return VoucherBalanceResult(rows, company.default_currency_id)
