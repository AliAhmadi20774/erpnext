"""Read-only General Ledger report for the currently supported dimensions."""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q, Sum

from organizations.models import Company
from parties.models import Customer, Supplier
from projects.models import Project

from .models import Account, CostCenter, FinanceBook, GLEntry


@dataclass(frozen=True)
class GeneralLedgerRow:
    entry: GLEntry
    running_balance: Decimal
    running_balance_in_account_currency: Decimal | None = None
    running_balance_in_group: Decimal | None = None
    debit_override: Decimal | None = None
    credit_override: Decimal | None = None
    account_debit_override: Decimal | None = None
    account_credit_override: Decimal | None = None
    source_count: int = 1

    @property
    def debit(self):
        return self.entry.debit if self.debit_override is None else self.debit_override

    @property
    def credit(self):
        return self.entry.credit if self.credit_override is None else self.credit_override

    @property
    def debit_in_account_currency(self):
        return self.entry.debit_in_account_currency if self.account_debit_override is None else self.account_debit_override

    @property
    def credit_in_account_currency(self):
        return self.entry.credit_in_account_currency if self.account_credit_override is None else self.account_credit_override


@dataclass(frozen=True)
class GeneralLedgerAccountGroup:
    account: Account
    rows: tuple[GeneralLedgerRow, ...]
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal


@dataclass(frozen=True)
class GeneralLedgerPartyGroup:
    party_type: str
    party: str
    label: str
    rows: tuple[GeneralLedgerRow, ...]
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal


@dataclass(frozen=True)
class GeneralLedgerVoucherGroup:
    voucher_type: str
    voucher_no: str
    label: str
    rows: tuple[GeneralLedgerRow, ...]
    period_debit: Decimal
    period_credit: Decimal


@dataclass(frozen=True)
class GeneralLedgerResult:
    rows: tuple[GeneralLedgerRow, ...]
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal
    account_currency: str | None = None
    opening_debit_in_account_currency: Decimal | None = None
    opening_credit_in_account_currency: Decimal | None = None
    period_debit_in_account_currency: Decimal | None = None
    period_credit_in_account_currency: Decimal | None = None
    closing_debit_in_account_currency: Decimal | None = None
    closing_credit_in_account_currency: Decimal | None = None
    groups: tuple[GeneralLedgerAccountGroup, ...] = ()
    group_by_account: bool = False
    party_groups: tuple[GeneralLedgerPartyGroup, ...] = ()
    group_by_party: bool = False
    voucher_groups: tuple[GeneralLedgerVoucherGroup, ...] = ()
    group_by_voucher: bool = False
    consolidate_vouchers: bool = False
    show_remarks: bool = False


def _totals(queryset, debit_field="debit", credit_field="credit"):
    values = queryset.aggregate(debit=Sum(debit_field), credit=Sum(credit_field))
    return values["debit"] or Decimal("0"), values["credit"] or Decimal("0")


def _party_key(customer_id, supplier_id):
    if customer_id:
        return "Customer", customer_id
    if supplier_id:
        return "Supplier", supplier_id
    return "No party", ""


def _consolidate_rows(rows, opening_by_account, opening_account_balance=None):
    consolidated = {}
    for row in rows:
        entry = row.entry
        key = (
            entry.posting_date, entry.voucher_type, entry.voucher_no, entry.account_id,
            entry.customer_id, entry.supplier_id, entry.cost_center_id, entry.project_id,
            entry.finance_book_id, entry.against_voucher_type, entry.against_voucher,
        )
        if key not in consolidated:
            consolidated[key] = {"entry": entry, "debit": Decimal("0"), "credit": Decimal("0"),
                                 "account_debit": Decimal("0"), "account_credit": Decimal("0"), "count": 0}
        total = consolidated[key]
        total["debit"] += row.debit
        total["credit"] += row.credit
        total["account_debit"] += row.debit_in_account_currency
        total["account_credit"] += row.credit_in_account_currency
        total["count"] += 1
    balances = {account_id: debit - credit for account_id, (debit, credit) in opening_by_account.items()}
    account_balance = opening_account_balance
    result = []
    for total in consolidated.values():
        entry = total["entry"]
        balance = balances.get(entry.account_id, Decimal("0")) + total["debit"] - total["credit"]
        balances[entry.account_id] = balance
        if account_balance is not None:
            account_balance += total["account_debit"] - total["account_credit"]
        result.append(GeneralLedgerRow(
            entry=entry, running_balance=balance, running_balance_in_account_currency=account_balance,
            debit_override=total["debit"], credit_override=total["credit"],
            account_debit_override=total["account_debit"], account_credit_override=total["account_credit"],
            source_count=total["count"],
        ))
    return result


def general_ledger_report(*, company, from_date, to_date, account=None, cost_center=None, project=None,
                          finance_book=None, include_default_book_entries=True,
                          show_opening_entries=False, party_type="", party="", voucher_no="",
                          against_voucher_no="", print_in_account_currency=False, group_by_account=False,
                          group_by_party=False, group_by_voucher=False, consolidate_vouchers=False,
                          disable_opening_balance_calculation=False, show_remarks=False):
    """Return opening, period, and closing totals plus per-account running rows."""
    if not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    company = Company.objects.get(pk=company.pk)
    if not isinstance(from_date, date) or not isinstance(to_date, date) or from_date > to_date:
        raise ValidationError("Enter a valid From Date and To Date range.")
    if account is not None and (not isinstance(account, Account) or account.company_id != company.pk):
        raise ValidationError("Account must belong to the selected company.")
    if cost_center is not None and (not isinstance(cost_center, CostCenter) or cost_center.company_id != company.pk):
        raise ValidationError("Cost center must belong to the selected company.")
    if project is not None and (not isinstance(project, Project) or project.company_id != company.pk):
        raise ValidationError("Project must belong to the selected company.")
    if finance_book is not None and not isinstance(finance_book, FinanceBook):
        raise TypeError("finance_book must be a FinanceBook instance")
    if party_type not in ("", "Customer", "Supplier"):
        raise ValidationError("Select Customer or Supplier as Party Type.")
    if not all(isinstance(value, str) for value in (party, voucher_no, against_voucher_no)):
        raise TypeError("Party and voucher numbers must be strings.")
    party, voucher_no, against_voucher_no = party.strip(), voucher_no.strip(), against_voucher_no.strip()
    if party and not party_type:
        raise ValidationError("Select a Party Type when filtering by Party.")
    if party and not {"Customer": Customer, "Supplier": Supplier}[party_type].objects.filter(pk=party).exists():
        raise ValidationError("The selected Party does not exist.")
    if include_default_book_entries and finance_book and company.default_finance_book_id:
        if finance_book.pk != company.default_finance_book_id:
            raise ValidationError("Uncheck Include Default Book Entries to select a different finance book.")
    if account is not None:
        account = Account.objects.get(pk=account.pk)
    if print_in_account_currency and (account is None or account.is_group):
        raise ValidationError("Select a leaf account to show totals in account currency.")
    if group_by_account and account is not None and not account.is_group:
        raise ValidationError("Select a group account or all accounts when grouping by account.")
    if sum((bool(group_by_account), bool(group_by_party), bool(group_by_voucher), bool(consolidate_vouchers))) > 1:
        raise ValidationError("Select only one grouping mode.")
    if (group_by_party or group_by_voucher) and print_in_account_currency:
        raise ValidationError("Account-currency totals are not supported with this grouping mode.")
    if group_by_voucher and voucher_no:
        raise ValidationError("Voucher number cannot be filtered when grouping by voucher.")
    if cost_center is not None:
        cost_center = CostCenter.objects.get(pk=cost_center.pk)
    if project is not None:
        project = Project.objects.get(pk=project.pk)

    selected_book_id = (finance_book.pk if finance_book else company.default_finance_book_id) if include_default_book_entries else (
        finance_book.pk if finance_book else None
    )
    entries = GLEntry.objects.filter(company=company, posting_date__lte=to_date, is_cancelled=False)
    if selected_book_id:
        entries = entries.filter(Q(finance_book_id=selected_book_id) | Q(finance_book__isnull=True))
    else:
        entries = entries.filter(finance_book__isnull=True)
    if account is not None:
        entries = entries.filter(account__company=company, account__lft__gte=account.lft, account__rgt__lte=account.rgt)
    if cost_center is not None:
        entries = entries.filter(cost_center__company=company, cost_center__lft__gte=cost_center.lft,
                                 cost_center__rgt__lte=cost_center.rgt)
    if project is not None:
        entries = entries.filter(project=project)
    if party_type:
        party_field = "customer_id" if party_type == "Customer" else "supplier_id"
        entries = entries.filter(**({party_field: party} if party else {f"{party_field}__isnull": False}))
    if voucher_no:
        entries = entries.filter(voucher_no=voucher_no)
    if against_voucher_no:
        entries = entries.filter(against_voucher=against_voucher_no)
    if disable_opening_balance_calculation:
        entries = entries.filter(Q(posting_date__gte=from_date) | Q(is_opening=True))

    opening = entries.filter(posting_date__lt=from_date)
    current = entries.filter(posting_date__gte=from_date)
    if not disable_opening_balance_calculation and not show_opening_entries:
        opening = opening | current.filter(is_opening=True)
        current = current.filter(is_opening=False)
    opening_debit, opening_credit = _totals(opening)
    period_debit, period_credit = _totals(current)
    if print_in_account_currency:
        opening_account_debit, opening_account_credit = _totals(
            opening, "debit_in_account_currency", "credit_in_account_currency",
        )
        period_account_debit, period_account_credit = _totals(
            current, "debit_in_account_currency", "credit_in_account_currency",
        )
        account_balance = opening_account_debit - opening_account_credit
    else:
        opening_account_debit = opening_account_credit = None
        period_account_debit = period_account_credit = None
        account_balance = None
    opening_by_account = {
        item["account_id"]: (item["debit"] or Decimal("0"), item["credit"] or Decimal("0"))
        for item in opening.values("account_id").annotate(debit=Sum("debit"), credit=Sum("credit"))
    }
    balances = {account_id: debit - credit for account_id, (debit, credit) in opening_by_account.items()}
    rows = []
    group_data = {}
    ordering = ("account_id", "posting_date", "id") if group_by_account else ("posting_date", "account_id", "id")
    for entry in current.select_related("account", "cost_center", "finance_book", "customer", "supplier").order_by(*ordering):
        balance = balances.get(entry.account_id, Decimal("0")) + entry.debit - entry.credit
        balances[entry.account_id] = balance
        if print_in_account_currency:
            account_balance += entry.debit_in_account_currency - entry.credit_in_account_currency
        row = GeneralLedgerRow(entry=entry, running_balance=balance,
                               running_balance_in_account_currency=account_balance)
        rows.append(row)
        if group_by_account:
            if entry.account_id not in group_data:
                opening_dr, opening_cr = opening_by_account.get(entry.account_id, (Decimal("0"), Decimal("0")))
                group_data[entry.account_id] = {
                    "account": entry.account, "rows": [], "opening_debit": opening_dr, "opening_credit": opening_cr,
                    "period_debit": Decimal("0"), "period_credit": Decimal("0"),
                }
            group = group_data[entry.account_id]
            group["rows"].append(row)
            group["period_debit"] += entry.debit
            group["period_credit"] += entry.credit
    groups = tuple(
        GeneralLedgerAccountGroup(
            account=data["account"], rows=tuple(data["rows"]),
            opening_debit=data["opening_debit"], opening_credit=data["opening_credit"],
            period_debit=data["period_debit"], period_credit=data["period_credit"],
            closing_debit=data["opening_debit"] + data["period_debit"],
            closing_credit=data["opening_credit"] + data["period_credit"],
        ) for data in group_data.values()
    )
    if consolidate_vouchers:
        rows = _consolidate_rows(rows, opening_by_account,
                                 opening_account_debit - opening_account_credit if print_in_account_currency else None)
    party_groups = ()
    if group_by_party:
        opening_by_party = {
            _party_key(item["customer_id"], item["supplier_id"]):
                (item["debit"] or Decimal("0"), item["credit"] or Decimal("0"))
            for item in opening.values("customer_id", "supplier_id").annotate(debit=Sum("debit"), credit=Sum("credit"))
        }
        party_data = {}
        for row in rows:
            key = _party_key(row.entry.customer_id, row.entry.supplier_id)
            if key not in party_data:
                opening_dr, opening_cr = opening_by_party.get(key, (Decimal("0"), Decimal("0")))
                party_data[key] = {
                    "rows": [], "opening_debit": opening_dr, "opening_credit": opening_cr,
                    "period_debit": Decimal("0"), "period_credit": Decimal("0"),
                    "balance": opening_dr - opening_cr,
                }
            data = party_data[key]
            data["period_debit"] += row.entry.debit
            data["period_credit"] += row.entry.credit
            data["balance"] += row.entry.debit - row.entry.credit
            data["rows"].append(replace(row, running_balance_in_group=data["balance"]))
        party_groups = tuple(
            GeneralLedgerPartyGroup(
                party_type=party_type, party=party_id,
                label=f"{party_type}: {party_id}" if party_id else "No party",
                rows=tuple(data["rows"]), opening_debit=data["opening_debit"],
                opening_credit=data["opening_credit"], period_debit=data["period_debit"],
                period_credit=data["period_credit"],
                closing_debit=data["opening_debit"] + data["period_debit"],
                closing_credit=data["opening_credit"] + data["period_credit"],
            ) for (party_type, party_id), data in party_data.items()
        )
    voucher_groups = ()
    if group_by_voucher:
        voucher_data = {}
        for row in rows:
            key = row.entry.voucher_type, row.entry.voucher_no
            if key not in voucher_data:
                voucher_data[key] = {"rows": [], "period_debit": Decimal("0"),
                                     "period_credit": Decimal("0"), "balance": Decimal("0")}
            data = voucher_data[key]
            data["period_debit"] += row.entry.debit
            data["period_credit"] += row.entry.credit
            data["balance"] += row.entry.debit - row.entry.credit
            data["rows"].append(replace(row, running_balance_in_group=data["balance"]))
        voucher_groups = tuple(
            GeneralLedgerVoucherGroup(
                voucher_type=voucher_type, voucher_no=number, label=f"{voucher_type}: {number}",
                rows=tuple(data["rows"]), period_debit=data["period_debit"],
                period_credit=data["period_credit"],
            ) for (voucher_type, number), data in voucher_data.items()
        )
    return GeneralLedgerResult(
        rows=tuple(rows), opening_debit=opening_debit, opening_credit=opening_credit,
        period_debit=period_debit, period_credit=period_credit,
        closing_debit=opening_debit + period_debit,
        closing_credit=opening_credit + period_credit,
        account_currency=account.account_currency_id if print_in_account_currency else None,
        opening_debit_in_account_currency=opening_account_debit,
        opening_credit_in_account_currency=opening_account_credit,
        period_debit_in_account_currency=period_account_debit,
        period_credit_in_account_currency=period_account_credit,
        closing_debit_in_account_currency=(opening_account_debit + period_account_debit) if print_in_account_currency else None,
        closing_credit_in_account_currency=(opening_account_credit + period_account_credit) if print_in_account_currency else None,
        groups=groups, group_by_account=group_by_account,
        party_groups=party_groups, group_by_party=group_by_party,
        voucher_groups=voucher_groups, group_by_voucher=group_by_voucher,
        consolidate_vouchers=consolidate_vouchers,
        show_remarks=show_remarks,
    )
