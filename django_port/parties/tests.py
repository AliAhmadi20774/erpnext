from datetime import date

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import ProtectedError
from django.test import TestCase

from catalog.models import Item, ItemGroup, ItemPrice, PriceList, UnitOfMeasure, find_item_price
from geo.models import Country, Currency

from .models import Customer, CustomerGroup, Supplier, SupplierGroup, Territory


class PartyTreeTests(TestCase):
    def test_customer_group_tree_reparents_and_rejects_cycles(self):
        root = CustomerGroup.objects.create(name="All Customer Groups", is_group=True)
        branch = CustomerGroup.objects.create(name="Business", parent_customer_group=root, is_group=True)
        leaf = CustomerGroup.objects.create(name="Retail", parent_customer_group=branch)
        branch.refresh_from_db()
        root.refresh_from_db()
        self.assertEqual(list(branch.descendants().values_list("name", flat=True)), ["Retail"])
        self.assertEqual((root.lft, root.rgt), (1, 6))
        with self.assertRaises(ValidationError):
            branch.parent_customer_group = leaf
            branch.save()
        branch.refresh_from_db()
        with self.assertRaises(ValidationError):
            branch.is_group = False
            branch.save()
        branch.refresh_from_db()
        with self.assertRaises(ValidationError):
            CustomerGroup.objects.filter(pk=leaf.pk).update(parent_customer_group=root)
        leaf.parent_customer_group = root
        leaf.save()
        self.assertEqual((root.lft, root.rgt), (1, 6))
        with self.assertRaises(ValidationError):
            branch.parent_customer_group = leaf
            branch.save()
        with self.assertRaises(ValidationError):
            root.delete()

    def test_seed_command_is_idempotent(self):
        call_command("seed_party_groups", verbosity=0)
        call_command("seed_party_groups", verbosity=0)
        self.assertEqual(CustomerGroup.objects.count(), 5)
        self.assertEqual(SupplierGroup.objects.count(), 8)
        self.assertEqual(Territory.objects.count(), 2)
        self.assertEqual(CustomerGroup.objects.get(name="Commercial").parent_customer_group_id, "All Customer Groups")
        self.assertEqual(SupplierGroup.objects.get(name="Local").parent_supplier_group_id, "All Supplier Groups")


class PartyTests(TestCase):
    def setUp(self):
        currency = Currency.objects.create(name="USD", enabled=True)
        self.selling = PriceList.objects.create(name="Selling", currency=currency, selling=True)
        self.buying = PriceList.objects.create(name="Buying", currency=currency, buying=True)
        root = CustomerGroup.objects.create(name="All Customer Groups", is_group=True)
        self.group = CustomerGroup.objects.create(name="Commercial", parent_customer_group=root)
        self.supplier_group = SupplierGroup.objects.create(name="All Supplier Groups", is_group=True)
        self.territory = Territory.objects.create(name="All Territories", is_group=True)
        self.country = Country.objects.create(name="United States", code="US")

    def test_customer_links_leaf_group_territory_and_selling_list(self):
        customer = Customer.objects.create(
            customer_name="  Acme  ", customer_group=self.group,
            territory=self.territory, default_price_list=self.selling,
            on_hold=True, release_date=date(2026, 10, 1),
        )
        self.assertEqual((customer.name, customer.customer_name), ("Acme", "Acme"))
        customer.on_hold = False
        customer.save()
        self.assertIsNone(customer.release_date)
        with self.assertRaises(ProtectedError):
            self.group.delete()
        with self.assertRaises(ValidationError):
            Customer.objects.create(customer_name="Wrong", customer_group=self.group, default_price_list=self.buying)
        with self.assertRaises(ValidationError):
            Customer.objects.create(customer_name="Root", customer_group=CustomerGroup.objects.get(name="All Customer Groups"))

    def test_supplier_country_buying_list_and_hold(self):
        supplier = Supplier.objects.create(
            supplier_name="Vendor", supplier_group=self.supplier_group,
            country=self.country, default_price_list=self.buying,
            on_hold=True, hold_type=Supplier.HoldType.INVOICES,
            release_date=date(2026, 10, 1),
        )
        self.assertEqual((supplier.name, supplier.hold_type), ("Vendor", "Invoices"))
        supplier.on_hold = False
        supplier.save()
        self.assertIsNone(supplier.release_date)
        with self.assertRaises(ValidationError):
            Supplier.objects.create(supplier_name="Wrong", default_price_list=self.selling)

    def test_price_lookup_accepts_party_record(self):
        customer = Customer.objects.create(
            name="CUST-0001", customer_name="Acme", customer_group=self.group
        )
        item_root = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        uom = UnitOfMeasure.objects.create(name="Nos")
        item = Item.objects.create(name="SKU-1", item_group=item_root, stock_uom=uom)
        price = ItemPrice.objects.create(
            item=item, price_list=self.selling, customer=customer, price_list_rate=10
        )
        self.assertEqual(find_item_price(item, self.selling, uom, customer=customer), price)
