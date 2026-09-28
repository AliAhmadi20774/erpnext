from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ItemPricePartyMigrationTests(TransactionTestCase):
    old_target = ("catalog", "0007_pricelist_itemprice_pricelistcountry")
    new_target = ("catalog", "0008_itemprice_legacy_customer_name_and_more")
    parties_target = ("parties", "0001_initial")

    def migrate_to(self, target):
        executor = MigrationExecutor(connection)
        targets = [target, self.parties_target]
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self.migrate_to(self.new_target)
        super().tearDown()

    def test_existing_party_names_are_linked_and_unresolved_names_survive_round_trip(self):
        old = self.migrate_to(self.old_target)
        Currency = old.get_model("geo", "Currency")
        UnitOfMeasure = old.get_model("catalog", "UnitOfMeasure")
        ItemGroup = old.get_model("catalog", "ItemGroup")
        Item = old.get_model("catalog", "Item")
        PriceList = old.get_model("catalog", "PriceList")
        ItemPrice = old.get_model("catalog", "ItemPrice")
        Customer = old.get_model("parties", "Customer")
        Supplier = old.get_model("parties", "Supplier")

        currency = Currency.objects.create(name="USD")
        uom = UnitOfMeasure.objects.create(name="Nos")
        group = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        item = Item.objects.create(name="SKU-1", item_group=group, stock_uom=uom)
        price_list = PriceList.objects.create(name="Both", currency=currency, buying=True, selling=True)
        Customer.objects.create(name="CUST-1", customer_name="Acme")
        Supplier.objects.create(name="SUP-1", supplier_name="Vendor")
        linked = ItemPrice.objects.create(
            item=item, price_list=price_list, currency=currency, uom=uom,
            price_list_rate=10, valid_from=date(2026, 1, 1),
            customer="CUST-1", supplier="SUP-1",
        )
        unresolved = ItemPrice.objects.create(
            item=item, price_list=price_list, currency=currency, uom=uom,
            price_list_rate=11, valid_from=date(2026, 2, 1), customer="OLD-CUST",
        )

        new = self.migrate_to(self.new_target)
        NewItemPrice = new.get_model("catalog", "ItemPrice")
        linked_new = NewItemPrice.objects.get(pk=linked.pk)
        unresolved_new = NewItemPrice.objects.get(pk=unresolved.pk)
        self.assertEqual((linked_new.customer_id, linked_new.supplier_id), ("CUST-1", "SUP-1"))
        self.assertEqual((linked_new.legacy_customer_name, linked_new.legacy_supplier_name), ("", ""))
        self.assertIsNone(unresolved_new.customer_id)
        self.assertEqual(unresolved_new.legacy_customer_name, "OLD-CUST")

        old_again = self.migrate_to(self.old_target)
        OldItemPrice = old_again.get_model("catalog", "ItemPrice")
        self.assertEqual(OldItemPrice.objects.get(pk=linked.pk).customer, "CUST-1")
        self.assertEqual(OldItemPrice.objects.get(pk=linked.pk).supplier, "SUP-1")
        self.assertEqual(OldItemPrice.objects.get(pk=unresolved.pk).customer, "OLD-CUST")
