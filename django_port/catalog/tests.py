import json
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import ProtectedError
from django.test import TestCase
from geo.models import Country, Currency
from parties.models import Customer, Supplier

from .models import (
    Item,
    ItemGroup,
    ItemPrice,
    ItemUOMConversion,
    PriceList,
    PriceListCountry,
    UOMCategory,
    UOMConversionFactor,
    UnitOfMeasure,
    get_uom_conv_factor,
    find_item_price,
)


class UOMImportTests(TestCase):
    def test_import_preserves_categories_flags_and_existing_edits(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "uoms.json"
            source.write_text(
                json.dumps(
                    [
                        {"uom_name": "Nos", "category": "Count", "must_be_whole_number": 1},
                        {"uom_name": "Kg", "category": "Weight", "symbol": "kg"},
                    ]
                ),
                encoding="utf-8",
            )
            call_command("import_erpnext_uoms", source=source, stdout=StringIO())
            self.assertEqual(UOMCategory.objects.count(), 2)
            self.assertTrue(UnitOfMeasure.objects.get(name="Nos").must_be_whole_number)
            self.assertEqual(UnitOfMeasure.objects.get(name="Kg").category_id, "Weight")

            UnitOfMeasure.objects.filter(name="Kg").update(symbol="custom")
            call_command("import_erpnext_uoms", source=source, stdout=StringIO())
            self.assertEqual(UnitOfMeasure.objects.count(), 2)
            self.assertEqual(UnitOfMeasure.objects.get(name="Kg").symbol, "custom")


class ItemGroupTreeTests(TestCase):
    def setUp(self):
        self.root = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        self.a = ItemGroup.objects.create(name="A", parent_item_group=self.root, is_group=True)
        self.b = ItemGroup.objects.create(name="B", parent_item_group=self.root, is_group=True)
        self.leaf = ItemGroup.objects.create(name="Leaf", parent_item_group=self.a)

    def test_nested_set_bounds_and_descendants_after_move(self):
        self.root.refresh_from_db()
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual((self.root.lft, self.root.rgt), (1, 8))
        self.assertTrue(self.a.lft < self.leaf.lft < self.leaf.rgt < self.a.rgt)
        self.assertEqual(list(self.a.descendants().values_list("name", flat=True)), ["Leaf"])

        self.leaf.parent_item_group = self.b
        self.leaf.save()
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(list(self.a.descendants()), [])
        self.assertEqual(list(self.b.descendants().values_list("name", flat=True)), ["Leaf"])

    def test_rejects_cycles_and_children_under_leaf(self):
        self.a.parent_item_group = self.leaf
        with self.assertRaises(ValidationError):
            self.a.save()
        self.a.refresh_from_db()
        self.a.is_group = False
        with self.assertRaises(ValidationError):
            self.a.save()
        with self.assertRaises(ValidationError):
            ItemGroup.objects.create(name="Invalid", parent_item_group=self.leaf)

    def test_delete_leaf_rebuilds_tree_and_group_with_children_is_protected(self):
        with self.assertRaises(ValidationError):
            self.a.delete()
        self.leaf.delete()
        self.root.refresh_from_db()
        self.assertEqual((self.root.lft, self.root.rgt), (1, 6))

    def test_queryset_changes_cannot_bypass_tree_updates(self):
        with self.assertRaises(ValidationError):
            ItemGroup.objects.filter(pk=self.a.pk).update(parent_item_group=self.b)
        ItemGroup.objects.filter(pk=self.leaf.pk).delete()
        self.root.refresh_from_db()
        self.assertEqual((self.root.lft, self.root.rgt), (1, 6))

    def test_seed_command_is_idempotent(self):
        call_command("seed_item_groups", stdout=StringIO())
        call_command("seed_item_groups", stdout=StringIO())
        self.assertEqual(ItemGroup.objects.filter(name="Default").count(), 1)
        self.assertEqual(ItemGroup.objects.get(name="Default").parent_item_group, self.root)


class ItemFoundationTests(TestCase):
    def test_item_uses_group_and_stock_uom_and_defaults_its_name(self):
        root = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        group = ItemGroup.objects.create(name="Products", parent_item_group=root)
        uom = UnitOfMeasure.objects.create(name="Nos")
        item = Item.objects.create(name="ABC-001", item_group=group, stock_uom=uom)
        self.assertEqual(item.item_name, "ABC-001")
        self.assertTrue(item.is_stock_item)
        with self.assertRaises(ProtectedError):
            group.delete()
        with self.assertRaises(ProtectedError):
            uom.delete()


class ItemUOMConversionTests(TestCase):
    def setUp(self):
        root = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        self.group = ItemGroup.objects.create(name="Products", parent_item_group=root)
        self.nos = UnitOfMeasure.objects.create(name="Nos")
        self.box = UnitOfMeasure.objects.create(name="Box")
        self.kg = UnitOfMeasure.objects.create(name="Kg")
        self.item = Item.objects.create(name="ABC-001", item_group=self.group, stock_uom=self.nos)

    def test_stock_unit_is_added_and_box_converts_to_stock_quantity(self):
        base = self.item.uom_conversions.get(uom=self.nos)
        self.assertEqual(base.conversion_factor, Decimal("1"))
        ItemUOMConversion.objects.create(
            item=self.item, uom=self.box, conversion_factor=Decimal("12")
        )
        self.assertEqual(self.item.quantity_in_stock_uom("2.5", self.box), Decimal("30"))
        self.assertEqual(self.item.quantity_in_stock_uom(3, "Nos"), Decimal("3"))

    def test_duplicate_unit_and_invalid_stock_factor_are_rejected(self):
        ItemUOMConversion.objects.create(item=self.item, uom=self.box, conversion_factor=12)
        with self.assertRaises(ValidationError):
            ItemUOMConversion.objects.create(item=self.item, uom=self.box, conversion_factor=10)
        base = self.item.uom_conversions.get(uom=self.nos)
        base.conversion_factor = Decimal("2")
        with self.assertRaises(ValidationError):
            base.save()
        with self.assertRaises(ValidationError):
            base.delete()
        with self.assertRaises(ValidationError):
            ItemUOMConversion.objects.filter(pk=base.pk).delete()
        with self.assertRaises(ValidationError):
            ItemUOMConversion.objects.filter(pk=base.pk).update(conversion_factor=2)
        base.uom = self.kg
        with self.assertRaises(ValidationError):
            base.save()

    def test_changing_stock_uom_replaces_conversions(self):
        ItemUOMConversion.objects.create(item=self.item, uom=self.box, conversion_factor=12)
        ItemUOMConversion.objects.create(item=self.item, uom=self.kg, conversion_factor=1000)
        self.item.stock_uom = self.kg
        self.item.save()
        self.assertEqual(list(self.item.uom_conversions.values_list("uom_id", flat=True)), ["Kg"])
        self.assertEqual(self.item.uom_conversions.get().conversion_factor, Decimal("1"))


class GlobalUOMConversionTests(TestCase):
    def setUp(self):
        self.category = UOMCategory.objects.create(name="Mass")
        for name in ("Gram", "Kg", "Milligram", "Bag 3 Kg", "Bag 25 Kg"):
            UnitOfMeasure.objects.create(name=name, category=self.category)

    def add_factor(self, source, target, value):
        return UOMConversionFactor.objects.create(
            category=self.category,
            from_uom_id=source,
            to_uom_id=target,
            value=Decimal(str(value)),
        )

    def test_exact_inverse_and_shared_source(self):
        self.add_factor("Gram", "Kg", Decimal("0.001"))
        self.add_factor("Gram", "Milligram", 1000)
        self.assertEqual(get_uom_conv_factor("Kg", "Gram"), Decimal("1000"))
        self.assertEqual(get_uom_conv_factor("Gram", "Kg"), Decimal("0.001"))
        self.assertEqual(get_uom_conv_factor("Kg", "Milligram"), Decimal("1000000"))
        self.assertEqual(get_uom_conv_factor("Kg", "Kg"), Decimal("1"))

    def test_shared_target_is_deterministic_and_skips_zero_divisor(self):
        self.add_factor("Bag 3 Kg", "Kg", 3)
        self.add_factor("Bag 25 Kg", "Kg", 0)
        self.assertIsNone(get_uom_conv_factor("Bag 3 Kg", "Bag 25 Kg"))
        self.add_factor("Bag 25 Kg", "Kg", 25)
        self.add_factor("Bag 3 Kg", "Kg", 6)
        self.add_factor("Bag 25 Kg", "Kg", 20)
        self.assertEqual(get_uom_conv_factor("Bag 3 Kg", "Bag 25 Kg"), Decimal("0.12"))

    def test_item_factor_defaults_from_global_but_custom_factor_wins(self):
        root = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        item = Item.objects.create(
            name="ABC-001",
            item_group=root,
            stock_uom=UnitOfMeasure.objects.get(name="Gram"),
        )
        self.add_factor("Kg", "Gram", 1000)
        conversion = ItemUOMConversion.objects.create(
            item=item, uom=UnitOfMeasure.objects.get(name="Kg")
        )
        self.assertEqual(conversion.conversion_factor, Decimal("1000"))
        conversion.conversion_factor = Decimal("900")
        conversion.save()
        self.assertEqual(item.quantity_in_stock_uom(2, "Kg"), Decimal("1800"))

    def test_real_erpnext_fixture_import_is_idempotent(self):
        call_command("import_erpnext_uoms", stdout=StringIO())
        call_command("import_erpnext_uom_conversions", stdout=StringIO())
        self.assertEqual(UOMConversionFactor.objects.count(), 235)
        self.assertEqual(UnitOfMeasure.objects.count(), 244)
        self.assertEqual(
            UOMConversionFactor.objects.get(from_uom_id="Meter", to_uom_id="Ells (UK)").value,
            Decimal("0.006993"),
        )
        call_command("import_erpnext_uom_conversions", stdout=StringIO())
        self.assertEqual(UOMConversionFactor.objects.count(), 235)


class PricingTests(TestCase):
    def setUp(self):
        group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        self.nos = UnitOfMeasure.objects.create(name="Nos")
        self.box = UnitOfMeasure.objects.create(name="Box")
        self.kg = UnitOfMeasure.objects.create(name="Kg")
        self.item = Item.objects.create(
            name="ABC-001", item_name="Widget", description="Test widget",
            item_group=group, stock_uom=self.nos,
        )
        ItemUOMConversion.objects.create(item=self.item, uom=self.box, conversion_factor=12)
        self.usd = Currency.objects.create(name="USD", enabled=True)
        self.eur = Currency.objects.create(name="EUR", enabled=True)
        self.price_list = PriceList.objects.create(name="Standard Selling", currency=self.usd, selling=True)
        self.customer = Customer.objects.create(name="ACME", customer_name="Acme")
        self.supplier = Supplier.objects.create(name="SUP-1", supplier_name="Supplier One")

    def add_price(self, rate, **kwargs):
        return ItemPrice.objects.create(
            item=self.item, price_list=self.price_list,
            price_list_rate=Decimal(str(rate)), **kwargs,
        )

    def test_price_list_flags_and_country(self):
        with self.assertRaises(ValidationError):
            PriceList.objects.create(name="Neither", currency=self.usd)
        country = Country.objects.create(name="United States", code="US")
        PriceListCountry.objects.create(price_list=self.price_list, country=country)
        self.assertEqual(self.price_list.countries.get().country, country)

    def test_item_price_derives_fields_and_price_list_updates_them(self):
        price = self.add_price(Decimal("13.50"), customer=self.customer, supplier=self.supplier)
        self.assertEqual(price.uom, self.nos)
        self.assertEqual(price.currency, self.usd)
        self.assertEqual((price.buying, price.selling), (False, True))
        self.assertEqual((price.item_name, price.item_description), ("Widget", "Test widget"))
        self.assertEqual((price.customer, price.supplier, price.reference), (self.customer, None, "ACME"))
        self.price_list.currency = self.eur
        self.price_list.buying = True
        self.price_list.save()
        price.refresh_from_db()
        self.assertEqual((price.currency, price.buying, price.selling), (self.eur, True, True))

    def test_invalid_price_conditions_and_duplicate(self):
        self.add_price(10, valid_from=date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            self.add_price(11, valid_from=date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            self.add_price(11, uom=self.kg)
        with self.assertRaises(ValidationError):
            self.add_price(11, valid_from=date(2026, 2, 1), valid_upto=date(2026, 1, 1))
        self.price_list.enabled = False
        self.price_list.save()
        with self.assertRaises(ValidationError):
            self.add_price(11, valid_from=date(2026, 2, 1))

    def test_lookup_date_party_batch_and_packing(self):
        general = self.add_price(10, valid_from=date(2026, 1, 1))
        party = self.add_price(12, valid_from=date(2026, 2, 1), customer=self.customer)
        batch = self.add_price(15, valid_from=date(2026, 2, 1), customer=self.customer, batch_no="B-1")
        self.assertEqual(find_item_price(self.item, self.price_list, self.nos, transaction_date=date(2026, 1, 15), customer="ACME"), general)
        self.assertEqual(find_item_price(self.item, self.price_list, self.nos, transaction_date=date(2026, 2, 15), customer="ACME"), party)
        self.assertEqual(find_item_price(self.item, self.price_list, self.nos, transaction_date=date(2026, 2, 15), customer="ACME", batch_no="B-1"), batch)
        self.assertIsNone(find_item_price(self.item, self.price_list, self.box, transaction_date=date(2026, 2, 15)))
        packed = self.add_price(120, uom=self.box, packing_unit=5, valid_from=date(2026, 1, 1))
        self.assertEqual(find_item_price(self.item, self.price_list, self.box, transaction_date=date(2026, 2, 15), quantity=10), packed)
        self.assertIsNone(find_item_price(self.item, self.price_list, self.box, transaction_date=date(2026, 2, 15), quantity=7))

    def test_legacy_party_name_remains_searchable_and_prevents_duplicate(self):
        price = self.add_price(10, valid_from=date(2026, 1, 1))
        ItemPrice.objects.filter(pk=price.pk).update(legacy_customer_name="OLD-CUST")
        self.assertEqual(find_item_price(self.item, self.price_list, self.nos, transaction_date=date(2026, 2, 1), customer="OLD-CUST"), price)
        old_customer = Customer.objects.create(name="OLD-CUST", customer_name="Legacy customer")
        with self.assertRaises(ValidationError):
            self.add_price(20, valid_from=date(2026, 1, 1), customer=old_customer)
        call_command("reconcile_item_price_parties", stdout=StringIO())
        call_command("reconcile_item_price_parties", stdout=StringIO())
        price.refresh_from_db()
        self.assertEqual(price.customer, old_customer)
        self.assertEqual(price.legacy_customer_name, "")

    def test_supplier_price_uses_party_relation_and_protects_record(self):
        buying_list = PriceList.objects.create(name="Buying", currency=self.usd, buying=True)
        price = ItemPrice.objects.create(
            item=self.item, price_list=buying_list, supplier=self.supplier,
            price_list_rate=Decimal("8"),
        )
        self.assertEqual(price.reference, self.supplier.name)
        self.assertEqual(find_item_price(self.item, buying_list, self.nos, supplier=self.supplier), price)
        with self.assertRaises(ProtectedError):
            self.supplier.delete()
