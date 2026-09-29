from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from organizations.models import Company
from stock.models import Warehouse, WarehouseType


class Command(BaseCommand):
    help = "Create ERPNext-style default warehouses for an existing company."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        company = Company.objects.select_for_update().filter(pk=options["company"]).first()
        if company is None:
            raise CommandError(f"Company does not exist: {options['company']}")
        if company.is_group:
            raise CommandError("Default warehouses cannot be created for a group company.")

        transit_type, _ = WarehouseType.objects.get_or_create(name="Transit")
        root = self._ensure_warehouse(company, "All Warehouses", is_group=True)
        stores = self._ensure_warehouse(company, "Stores", parent=root)
        self._ensure_warehouse(company, "Work In Progress", parent=root)
        self._ensure_warehouse(company, "Finished Goods", parent=root)
        transit = self._ensure_warehouse(
            company, "Goods In Transit", parent=root, warehouse_type=transit_type
        )

        changed = []
        if not company.default_warehouse_id:
            company.default_warehouse = stores
            changed.append("default_warehouse")
        if not company.default_in_transit_warehouse_id:
            company.default_in_transit_warehouse = transit
            changed.append("default_in_transit_warehouse")
        if changed:
            company.save(update_fields=changed)

        self.stdout.write(
            self.style.SUCCESS(
                f"Root: {root.name}; default: {stores.name}; transit: {transit.name}"
            )
        )

    @staticmethod
    def _ensure_warehouse(company, warehouse_name, *, parent=None, is_group=False, warehouse_type=None):
        existing = Warehouse.objects.filter(
            company=company, warehouse_name=warehouse_name
        ).first()
        if existing:
            expected = {
                "parent_warehouse_id": parent.pk if parent else None,
                "is_group": is_group,
                "disabled": False,
                "warehouse_type_id": warehouse_type.pk if warehouse_type else None,
            }
            if any(getattr(existing, field) != value for field, value in expected.items()):
                raise CommandError(
                    f"Existing warehouse has a different structure: {existing.name}"
                )
            return existing

        warehouse = Warehouse(
            warehouse_name=warehouse_name,
            company=company,
            parent_warehouse=parent,
            is_group=is_group,
            warehouse_type=warehouse_type,
        )
        warehouse.save(validate_inventory_account=False)
        return warehouse
