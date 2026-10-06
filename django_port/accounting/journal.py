"""Submit and cancel the supported Journal Entry types through the shared GL service."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from organizations.models import Company

from .ledger import LedgerLine, make_reverse_gl_entries, post_gl_entries
from .models import JournalEntry, PeriodClosingVoucher
from .periods import validate_accounting_period


@transaction.atomic
def submit_journal_entry(journal, *, user=None):
    if not isinstance(journal, JournalEntry) or not journal.pk:
        raise TypeError("journal must be a saved JournalEntry")
    company = Company.objects.select_for_update().get(pk=journal.company_id)
    journal = JournalEntry.objects.select_for_update().get(pk=journal.pk)
    if journal.status != JournalEntry.Status.DRAFT:
        raise ValidationError("Journal entry has already been submitted.")
    journal.full_clean()
    rows = list(journal.accounts.select_related("account", "cost_center", "project", "customer", "supplier").order_by("position", "id"))
    if len(rows) < 2:
        raise ValidationError("A journal entry requires at least two account rows.")
    lines = []
    for row in rows:
        row.full_clean()
        amount_debit = row.debit_in_account_currency
        amount_credit = row.credit_in_account_currency
        foreign = row.account.account_currency_id != company.default_currency_id
        if foreign and not journal.multi_currency:
            raise ValidationError("Enable Multi Currency for foreign-currency accounts.")
        if not foreign and row.exchange_rate not in (None, Decimal("1")):
            raise ValidationError("Company-currency accounts must use exchange rate 1.")
        remarks = "\n".join(value for value in (row.user_remark.strip(), journal.remark.strip()) if value)
        lines.append(LedgerLine(
            account=row.account,
            debit=Decimal("0") if foreign else amount_debit,
            credit=Decimal("0") if foreign else amount_credit,
            debit_in_account_currency=amount_debit,
            credit_in_account_currency=amount_credit,
            exchange_rate=row.exchange_rate,
            customer=row.customer, supplier=row.supplier,
            cost_center=row.cost_center, project=row.project, finance_book=journal.finance_book,
            against_voucher_type=row.reference_type, against_voucher=row.reference_name,
            remarks=remarks,
        ))
    entries = post_gl_entries(
        company=company, posting_date=journal.posting_date,
        voucher_type="Journal Entry", voucher_no=journal.name,
        lines=lines, is_opening=journal.voucher_type == JournalEntry.VoucherType.OPENING_ENTRY,
        user=user,
    )
    journal.total_debit = sum((entry.debit for entry in entries), Decimal("0"))
    journal.total_credit = sum((entry.credit for entry in entries), Decimal("0"))
    journal.status = JournalEntry.Status.SUBMITTED
    journal._submitting = True
    journal.save(update_fields=("total_debit", "total_credit", "status"))
    return journal


@transaction.atomic
def cancel_journal_entry(journal, *, user=None):
    """Cancel a submitted Journal Entry and reverse its GL entries."""
    if not isinstance(journal, JournalEntry) or not journal.pk:
        raise TypeError("journal must be a saved JournalEntry")
    company = Company.objects.select_for_update().get(pk=journal.company_id)
    journal = JournalEntry.objects.select_for_update().get(pk=journal.pk)
    if journal.status != JournalEntry.Status.SUBMITTED:
        raise ValidationError("Only submitted journal entries can be cancelled.")

    if PeriodClosingVoucher.objects.filter(
        company=company,
        status=PeriodClosingVoucher.Status.SUBMITTED,
        period_end_date__gte=journal.posting_date,
    ).exists():
        raise ValidationError("You cannot cancel transactions on or before a closed period closing date.")

    validate_accounting_period(
        company=company,
        posting_date=journal.posting_date,
        document_type="Journal Entry",
        user=user,
    )

    make_reverse_gl_entries(
        voucher_type="Journal Entry",
        voucher_no=journal.name,
        company=company,
        posting_date=journal.posting_date,
        user=user,
    )

    journal.status = JournalEntry.Status.CANCELLED
    journal._cancelling = True
    journal.save(update_fields=("status",))
    return journal

