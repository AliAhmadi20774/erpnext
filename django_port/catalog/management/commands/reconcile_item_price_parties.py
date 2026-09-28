from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import ItemPrice
from parties.models import Customer, Supplier


class Command(BaseCommand):
    help = "Link legacy Item Price party names to imported customers and suppliers."

    @transaction.atomic
    def handle(self, *args, **options):
        linked_customers = 0
        linked_suppliers = 0
        for price in ItemPrice.objects.filter(customer__isnull=True).exclude(legacy_customer_name="").iterator():
            if Customer.objects.filter(pk=price.legacy_customer_name).exists():
                ItemPrice.objects.filter(pk=price.pk).update(
                    customer_id=price.legacy_customer_name, legacy_customer_name=""
                )
                linked_customers += 1
        for price in ItemPrice.objects.filter(supplier__isnull=True).exclude(legacy_supplier_name="").iterator():
            if Supplier.objects.filter(pk=price.legacy_supplier_name).exists():
                ItemPrice.objects.filter(pk=price.pk).update(
                    supplier_id=price.legacy_supplier_name, legacy_supplier_name=""
                )
                linked_suppliers += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Linked {linked_customers} customers and {linked_suppliers} suppliers."
            )
        )
