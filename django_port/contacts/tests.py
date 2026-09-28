from django.core.exceptions import ValidationError
from django.contrib import admin
from django.db.models import ProtectedError
from django.test import RequestFactory
from django.test import TestCase

from geo.models import Country
from parties.admin import CustomerAdmin
from parties.forms import CustomerQuickEntryForm
from parties.models import Customer, PartyType, Supplier

from .models import Address, AddressPartyLink, Contact, ContactEmail, ContactPartyLink, ContactPhone
from .services import create_primary_address, create_primary_contact


class AddressContactTests(TestCase):
    def setUp(self):
        self.country = Country.objects.create(name="United States", code="US")
        self.customer = Customer.objects.create(name="CUST-1", customer_name="Acme")
        self.other_customer = Customer.objects.create(name="CUST-2", customer_name="Another")
        self.supplier = Supplier.objects.create(name="SUP-1", supplier_name="Vendor")
        self.address = Address.objects.create(
            name="ADDR-1", address_title="Main office", address_type=Address.Type.BILLING,
            address_line1="1 Main Street", city="Boston", country=self.country,
        )
        self.contact = Contact.objects.create(name="CONT-1", first_name="Ada", last_name="Lovelace")

    def test_shared_links_and_primary_selection(self):
        customer_address_link = AddressPartyLink.objects.create(address=self.address, customer=self.customer)
        AddressPartyLink.objects.create(address=self.address, supplier=self.supplier)
        customer_contact_link = ContactPartyLink.objects.create(contact=self.contact, customer=self.customer)
        ContactPartyLink.objects.create(contact=self.contact, supplier=self.supplier)
        self.customer.customer_primary_address = self.address
        self.customer.customer_primary_contact = self.contact
        self.customer.save()
        self.supplier.supplier_primary_address = self.address
        self.supplier.supplier_primary_contact = self.contact
        self.supplier.save()
        self.assertTrue(Address.objects.get(pk=self.address.pk).is_primary_address)
        self.assertTrue(Contact.objects.get(pk=self.contact.pk).is_primary_contact)
        with self.assertRaises(ValidationError):
            customer_address_link.delete()
        with self.assertRaises(ValidationError):
            customer_contact_link.delete()
        customer_address_link.customer = self.other_customer
        with self.assertRaises(ValidationError):
            customer_address_link.save()
        with self.assertRaises(ValidationError):
            AddressPartyLink.objects.filter(pk=customer_address_link.pk).update(customer=self.other_customer)
        self.address.disabled = True
        with self.assertRaises(ValidationError):
            self.address.save()
        with self.assertRaises(ProtectedError):
            self.address.delete()

    def test_unlinked_or_disabled_primary_records_are_rejected(self):
        self.customer.customer_primary_address = self.address
        with self.assertRaises(ValidationError):
            self.customer.save()
        self.customer.customer_primary_address = None
        self.customer.customer_primary_contact = self.contact
        with self.assertRaises(ValidationError):
            self.customer.save()
        self.customer.customer_primary_contact = None
        AddressPartyLink.objects.create(address=self.address, customer=self.customer)
        self.address.disabled = True
        self.address.save()
        self.customer.customer_primary_address = self.address
        with self.assertRaises(ValidationError):
            self.customer.save()

    def test_links_require_exactly_one_party_and_reject_duplicates(self):
        with self.assertRaises(ValidationError):
            AddressPartyLink.objects.create(address=self.address)
        with self.assertRaises(ValidationError):
            ContactPartyLink.objects.create(contact=self.contact, customer=self.customer, supplier=self.supplier)
        AddressPartyLink.objects.create(address=self.address, customer=self.customer)
        with self.assertRaises(ValidationError):
            AddressPartyLink.objects.create(address=self.address, customer=self.customer)
        ContactPartyLink.objects.create(contact=self.contact, customer=self.customer)
        ContactPartyLink.objects.create(contact=self.contact, customer=self.other_customer)

    def test_primary_email_and_phone_rows(self):
        ContactEmail.objects.create(contact=self.contact, email_id="ada@example.com", is_primary=True)
        ContactPhone.objects.create(contact=self.contact, phone="555-0100", is_primary_phone=True)
        ContactPhone.objects.create(contact=self.contact, phone="555-0200", is_primary_mobile_no=True)
        self.assertEqual(self.contact.email_id, "ada@example.com")
        self.assertEqual(self.contact.phone, "555-0100")
        self.assertEqual(self.contact.mobile_no, "555-0200")
        with self.assertRaises(ValidationError):
            ContactEmail.objects.create(contact=self.contact, email_id="other@example.com", is_primary=True)
        with self.assertRaises(ValidationError):
            ContactPhone.objects.create(contact=self.contact, phone="555-0300", is_primary_phone=True)
        with self.assertRaises(ValidationError):
            ContactEmail.objects.create(contact=self.contact, email_id="invalid-address", is_primary=False)

    def test_customer_primary_records_are_created_once_from_quick_entry(self):
        self.customer.customer_name = "Ada Byron Lovelace"
        self.customer.customer_type = PartyType.INDIVIDUAL
        self.customer.save()
        contact = create_primary_contact(
            self.customer, email_id="ada@example.com", mobile_no="555-0100"
        )
        address = create_primary_address(
            self.customer, address_line1="1 Main Street", city="Boston", country=self.country
        )
        self.customer.refresh_from_db()
        self.assertEqual((contact.first_name, contact.middle_name, contact.last_name), ("Ada", "Byron", "Lovelace"))
        self.assertEqual((contact.email_id, contact.mobile_no), ("ada@example.com", "555-0100"))
        self.assertEqual((self.customer.customer_primary_contact, self.customer.customer_primary_address), (contact, address))
        self.assertTrue(address.is_shipping_address)
        self.assertTrue(contact.party_links.filter(customer=self.customer).exists())
        self.assertTrue(address.party_links.filter(customer=self.customer).exists())
        self.assertEqual(create_primary_contact(self.customer, email_id="other@example.com"), contact)
        self.assertEqual(create_primary_address(self.customer, address_line1="Other", city="Boston", country=self.country), address)
        self.assertEqual(Contact.objects.count(), 2)
        self.assertEqual(Address.objects.count(), 2)

    def test_supplier_contact_and_invalid_data_roll_back(self):
        with self.assertRaises(ValidationError):
            create_primary_contact(self.supplier, email_id="invalid-address")
        self.assertFalse(ContactPartyLink.objects.filter(supplier=self.supplier).exists())
        with self.assertRaises(ValidationError):
            create_primary_address(self.supplier, address_line1="1 Main Street", country=self.country)
        self.assertFalse(AddressPartyLink.objects.filter(supplier=self.supplier).exists())
        contact = create_primary_contact(self.supplier, email_id="vendor@example.com")
        self.assertEqual(contact.company_name, "Vendor")
        self.assertEqual(contact.email_id, "vendor@example.com")
        address = create_primary_address(
            self.supplier, address_line1="2 Factory Road", city="Boston", country=self.country
        )
        self.supplier.refresh_from_db()
        self.assertEqual((self.supplier.supplier_primary_contact, self.supplier.supplier_primary_address), (contact, address))

    def test_admin_quick_entry_form_creates_linked_records(self):
        form = CustomerQuickEntryForm(data={
            "name": "CUST-3", "customer_name": "New Co", "customer_type": "Company",
            "new_contact_email": "new@example.com", "new_address_line1": "3 New Road",
            "new_address_city": "Boston", "new_address_country": self.country.pk,
        })
        self.assertTrue(form.is_valid(), form.errors)
        customer = form.save(commit=False)
        CustomerAdmin(Customer, admin.site).save_model(RequestFactory().get("/admin/"), customer, form, False)
        customer.refresh_from_db()
        self.assertEqual(customer.customer_primary_contact.email_id, "new@example.com")
        self.assertEqual(customer.customer_primary_address.city, "Boston")
        invalid = CustomerQuickEntryForm(data={
            "name": "CUST-4", "customer_name": "Invalid Co", "customer_type": "Company",
            "new_address_line1": "4 Missing City", "new_address_country": self.country.pk,
        })
        self.assertFalse(invalid.is_valid())
        self.assertIn("new_address_city", invalid.errors)

    def test_party_display_reads_current_contact_and_address_values(self):
        AddressPartyLink.objects.create(address=self.address, customer=self.customer)
        AddressPartyLink.objects.create(address=self.address, supplier=self.supplier)
        ContactPartyLink.objects.create(contact=self.contact, customer=self.customer)
        ContactPartyLink.objects.create(contact=self.contact, supplier=self.supplier)
        self.customer.customer_primary_contact = self.contact
        self.customer.customer_primary_address = self.address
        self.customer.save()
        self.supplier.supplier_primary_contact = self.contact
        self.supplier.supplier_primary_address = self.address
        self.supplier.save()
        email = ContactEmail.objects.create(contact=self.contact, email_id="ada@example.com")
        phone = ContactPhone.objects.create(contact=self.contact, phone="555-0100", is_primary_mobile_no=True)
        self.assertTrue(email.is_primary)
        self.assertEqual((self.customer.email_id, self.customer.mobile_no), ("ada@example.com", "555-0100"))
        self.assertEqual((self.customer.first_name, self.customer.last_name), ("Ada", "Lovelace"))
        self.assertIn("1 Main Street", self.customer.primary_address)
        self.assertEqual(self.supplier.email_id, "ada@example.com")
        email.email_id = "updated@example.com"
        email.save()
        phone.phone = "555-0200"
        phone.save()
        self.contact.first_name = "Grace"
        self.contact.save()
        self.address.address_line1 = "2 New Street"
        self.address.save()
        self.assertEqual((self.customer.email_id, self.customer.mobile_no, self.customer.first_name), ("updated@example.com", "555-0200", "Grace"))
        self.assertIn("2 New Street", self.supplier.primary_address)
        replacement = Contact.objects.create(name="CONT-2", first_name="New")
        ContactEmail.objects.create(contact=replacement, email_id="new@example.com")
        ContactPartyLink.objects.create(contact=replacement, customer=self.customer)
        self.customer.customer_primary_contact = replacement
        self.customer.save()
        self.assertEqual((self.customer.email_id, self.customer.first_name), ("new@example.com", "New"))
        self.assertEqual(self.supplier.email_id, "updated@example.com")

    def test_single_remaining_email_becomes_primary(self):
        first = ContactEmail.objects.create(contact=self.contact, email_id="first@example.com")
        second = ContactEmail.objects.create(contact=self.contact, email_id="second@example.com")
        self.assertTrue(first.is_primary)
        self.assertFalse(second.is_primary)
        ContactEmail.objects.filter(pk=first.pk).delete()
        second.refresh_from_db()
        self.assertTrue(second.is_primary)
        self.assertEqual(self.contact.email_id, "second@example.com")
