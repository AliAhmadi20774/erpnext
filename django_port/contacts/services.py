from django.core.exceptions import ValidationError
from django.db import transaction

from parties.models import Customer, Supplier

from .models import Address, AddressPartyLink, Contact, ContactEmail, ContactPartyLink, ContactPhone


def _party_details(party):
    if isinstance(party, Customer):
        return "customer", "customer_name", "customer_type", "customer_primary_contact", "customer_primary_address"
    if isinstance(party, Supplier):
        return "supplier", "supplier_name", "supplier_type", "supplier_primary_contact", "supplier_primary_address"
    raise TypeError("Expected a Customer or Supplier.")


def _available_name(model, base):
    name = base[:140]
    suffix = 1
    while model.objects.filter(pk=name).exists():
        suffix += 1
        ending = f"-{suffix}"
        name = f"{base[:140 - len(ending)]}{ending}"
    return name


def _split_name(full_name):
    parts = full_name.split()
    return parts[0], " ".join(parts[1:-1]), parts[-1] if len(parts) > 1 else ""


@transaction.atomic
def create_primary_contact(party, *, email_id="", mobile_no="", first_name="", last_name=""):
    source_party = party
    party_field, name_field, type_field, primary_field, _ = _party_details(party)
    party = type(party).objects.select_for_update().get(pk=party.pk)
    if getattr(party, f"{primary_field}_id"):
        return getattr(party, primary_field)
    email_id = email_id.strip()
    mobile_no = mobile_no.strip()
    first_name = first_name.strip()
    last_name = last_name.strip()
    if not any((email_id, mobile_no, first_name, last_name)):
        return None

    party_name = getattr(party, name_field)
    values = {}
    if getattr(party, type_field) == "Individual":
        first, middle, last = _split_name(party_name)
        values.update(first_name=first, middle_name=middle, last_name=last)
    else:
        values["company_name"] = party_name
    if first_name:
        values["first_name"] = first_name
    if last_name:
        values["last_name"] = last_name

    contact = Contact.objects.create(
        name=_available_name(Contact, f"{party.name}-Contact"),
        is_primary_contact=True,
        **values,
    )
    if email_id:
        ContactEmail.objects.create(contact=contact, email_id=email_id, is_primary=True)
    if mobile_no:
        ContactPhone.objects.create(contact=contact, phone=mobile_no, is_primary_mobile_no=True)
    ContactPartyLink.objects.create(contact=contact, **{party_field: party})
    setattr(party, primary_field, contact)
    party.save(update_fields=[primary_field])
    setattr(source_party, primary_field, contact)
    return contact


@transaction.atomic
def create_primary_address(
    party, *, address_line1="", city="", country=None,
    address_line2="", state="", pincode="", address_type=Address.Type.BILLING,
):
    source_party = party
    party_field, name_field, _, _, primary_field = _party_details(party)
    party = type(party).objects.select_for_update().get(pk=party.pk)
    if getattr(party, f"{primary_field}_id"):
        return getattr(party, primary_field)
    address_line1 = address_line1.strip()
    if not address_line1:
        return None
    if not city.strip() or not country:
        raise ValidationError("City and country are required to create a party address.")

    address = Address.objects.create(
        name=_available_name(Address, f"{party.name}-{address_type}"),
        address_title=getattr(party, name_field),
        address_type=address_type,
        address_line1=address_line1,
        address_line2=address_line2.strip(),
        city=city.strip(),
        state=state.strip(),
        pincode=pincode.strip(),
        country=country,
        is_primary_address=True,
        is_shipping_address=True,
    )
    AddressPartyLink.objects.create(address=address, **{party_field: party})
    setattr(party, primary_field, address)
    party.save(update_fields=[primary_field])
    setattr(source_party, primary_field, address)
    return address
