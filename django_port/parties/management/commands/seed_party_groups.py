from django.core.management.base import BaseCommand
from django.db import transaction

from parties.models import CustomerGroup, SupplierGroup, Territory


class Command(BaseCommand):
    help = "Create ERPNext's initial customer groups, supplier groups and territories."

    @transaction.atomic
    def handle(self, *args, **options):
        customer_root, _ = CustomerGroup.objects.get_or_create(
            name="All Customer Groups", defaults={"is_group": True}
        )
        for name in ("Individual", "Commercial", "Non Profit", "Government"):
            CustomerGroup.objects.get_or_create(
                name=name, defaults={"parent_customer_group": customer_root}
            )

        supplier_root, _ = SupplierGroup.objects.get_or_create(
            name="All Supplier Groups", defaults={"is_group": True}
        )
        for name in ("Services", "Local", "Raw Material", "Electrical", "Hardware", "Pharmaceutical", "Distributor"):
            SupplierGroup.objects.get_or_create(
                name=name, defaults={"parent_supplier_group": supplier_root}
            )

        territory_root, _ = Territory.objects.get_or_create(
            name="All Territories", defaults={"is_group": True}
        )
        Territory.objects.get_or_create(
            name="Rest Of The World", defaults={"parent_territory": territory_root}
        )
        self.stdout.write(self.style.SUCCESS("Party groups and territories are ready."))
