"""Customer and supplier Trial Balance over the supported GL party fields."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q, Sum

from organizations.models import Company
from parties.models import Customer, Supplier

from .models import Account, FiscalYear, GLEntry


ZERO = Decimal("0")
PARTY_MODELS = {"Customer": (Customer, "customer_id", "customer_name"),
                "Supplier": (Supplier, "supplier_id", "supplier_name")}


@dataclass(frozen=True)
class PartyTrialBalanceRow:
    party: str
    party_name: str
    opening_debit: Decimal
    opening_credit: Decimal
    debit: Decimal
    credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal


@dataclass(frozen=True)
class PartyTrialBalanceResult:
    rows: tuple[PartyTrialBalanceRow, ...]
    total_opening_debit: Decimal
    total_opening_credit: Decimal
    total_debit: Decimal
    total_credit: Decimal
    total_closing_debit: Decimal
    total_closing_credit: Decimal
    currency: str


def _net(debit, credit):
    balance = debit - credit
    return max(balance, ZERO), max(-balance, ZERO)


def _group_amounts(queryset, party_field):
    return {
        row[party_field]: (row["debit"] or ZERO, row["credit"] or ZERO)
        for row in queryset.values(party_field).annotate(debit=Sum("debit"), credit=Sum("credit"))
    }


def trial_balance_for_party_report(*, company, fiscal_year, from_date, to_date, party_type,
                                   party="", account=None, show_zero_values=False,
                                   exclude_zero_balance_parties=True):
    """Return per-party opening, period, and closing amounts in company currency."""
    if not isinstance(company, Company) or not isinstance(fiscal_year, FiscalYear):
        raise TypeError("Select a company and fiscal year.")
    company = Company.objects.get(pk=company.pk)
    if fiscal_year.disabled or (not fiscal_year.all_companies and
                                not fiscal_year.company_links.filter(company=company).exists()):
        raise ValidationError("Fiscal year does not apply to this company.")
    if (not isinstance(from_date, date) or not isinstance(to_date, date)
            or not fiscal_year.year_start_date <= from_date <= to_date <= fiscal_year.year_end_date):
        raise ValidationError("Select dates inside the fiscal year, with From Date before To Date.")
    if party_type not in PARTY_MODELS:
        raise ValidationError("Only Customer and Supplier parties are supported yet.")
    if not isinstance(party, str):
        raise TypeError("party must be a party ID string")
    party = party.strip()
    party_model, party_field, name_field = PARTY_MODELS[party_type]
    parties = party_model.objects.all().order_by("name")
    if party:
        parties = parties.filter(pk=party)
        if not parties.exists():
            raise ValidationError("The selected party does not exist.")
    if account is not None:
        if not isinstance(account, Account) or account.company_id != company.pk:
            raise ValidationError("Account must belong to the selected company.")
        account = Account.objects.get(pk=account.pk)

    entries = GLEntry.objects.filter(company=company, is_cancelled=False,
                                     posting_date__lte=to_date, **{f"{party_field}__isnull": False})
    if account is not None:
        entries = entries.filter(account__lft__gte=account.lft, account__rgt__lte=account.rgt,
                                 account__company=company)
    if party:
        entries = entries.filter(**{party_field: party})
    opening = _group_amounts(entries.filter(
        Q(posting_date__lt=from_date) | Q(is_opening=True),
    ), party_field)
    period = _group_amounts(entries.filter(posting_date__gte=from_date, is_opening=False), party_field)

    rows = []
    totals = [ZERO] * 6
    for item in parties:
        opening_dr, opening_cr = _net(*opening.get(item.pk, (ZERO, ZERO)))
        debit, credit = period.get(item.pk, (ZERO, ZERO))
        closing_dr, closing_cr = _net(opening_dr + debit, opening_cr + credit)
        if exclude_zero_balance_parties and not (closing_dr or closing_cr):
            continue
        if not show_zero_values and not any((opening_dr, opening_cr, debit, credit, closing_dr, closing_cr)):
            continue
        row = PartyTrialBalanceRow(item.pk, getattr(item, name_field), opening_dr, opening_cr,
                                   debit, credit, closing_dr, closing_cr)
        rows.append(row)
        for index, amount in enumerate((opening_dr, opening_cr, debit, credit, closing_dr, closing_cr)):
            totals[index] += amount
    return PartyTrialBalanceResult(tuple(rows), *totals, company.default_currency_id)
