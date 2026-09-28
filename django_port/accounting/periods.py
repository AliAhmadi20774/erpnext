"""Accounting-period restrictions corresponding to ERPNext's period closing hooks."""

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import AccountingPeriod, ClosedDocument


# erpnext/hooks.py: period_closing_doctypes
PERIOD_CLOSING_DOCUMENT_TYPES = (
    "Sales Invoice", "Purchase Invoice", "Journal Entry", "Bank Clearance",
    "Stock Entry", "Dunning", "Invoice Discounting", "Payment Entry",
    "Period Closing Voucher", "Process Deferred Accounting", "Asset",
    "Asset Capitalization", "Asset Repair", "Delivery Note",
    "Landed Cost Voucher", "Purchase Receipt", "Stock Reconciliation",
    "Subcontracting Receipt",
)


@transaction.atomic
def create_accounting_period(*, period_name, company, start_date, end_date, exempted_role=None,
                             closed_document_types=None):
    """Create a period with ERPNext's default closed document list."""
    period = AccountingPeriod(
        period_name=period_name, company=company, start_date=start_date,
        end_date=end_date, exempted_role=exempted_role,
    )
    period.save()
    document_types = PERIOD_CLOSING_DOCUMENT_TYPES if closed_document_types is None else tuple(closed_document_types)
    if len(set(document_types)) != len(document_types):
        raise ValidationError("A document type can occur only once in an accounting period.")
    for document_type in document_types:
        ClosedDocument.objects.create(accounting_period=period, document_type=document_type, closed=True)
    return period


def validate_accounting_period(*, company, posting_date, document_type, user=None):
    """Reject a closed document date unless the given user has its exempted role."""
    if document_type == "Bank Clearance" or document_type not in PERIOD_CLOSING_DOCUMENT_TYPES:
        return
    periods = AccountingPeriod.objects.filter(
        company=company, disabled=False,
        start_date__lte=posting_date, end_date__gte=posting_date,
        closed_documents__document_type=document_type,
        closed_documents__closed=True,
    ).select_related("exempted_role")
    for period in periods:
        if period.exempted_role_id and user is not None and getattr(user, "is_authenticated", False):
            if user.groups.filter(pk=period.exempted_role_id).exists():
                continue
        raise ValidationError(f"Cannot create {document_type} within closed accounting period {period.name}.")
