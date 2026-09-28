from parties.models import Customer, Supplier

from .models import PartyAccount


def resolve_party_account(party, company, *, advance=False):
    """Return the configured account using ERPNext's party, direct group, company order."""
    if isinstance(party, Customer):
        party_field = "customer"
        group_field = "customer_group"
        group_id = party.customer_group_id
        company_field = "default_advance_received_account" if advance else "default_receivable_account"
    elif isinstance(party, Supplier):
        party_field = "supplier"
        group_field = "supplier_group"
        group_id = party.supplier_group_id
        company_field = "default_advance_paid_account" if advance else "default_payable_account"
    else:
        raise TypeError("Party must be a Customer or Supplier instance.")

    account_field = "advance_account" if advance else "account"
    row = PartyAccount.objects.filter(company=company, **{party_field: party}).select_related(account_field).first()
    if row and getattr(row, f"{account_field}_id"):
        return getattr(row, account_field)
    if group_id:
        row = PartyAccount.objects.filter(company=company, **{f"{group_field}_id": group_id}).select_related(account_field).first()
        if row and getattr(row, f"{account_field}_id"):
            return getattr(row, account_field)
    return getattr(company, company_field)
