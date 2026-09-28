import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import UOMCategory, UOMConversionFactor, UnitOfMeasure


DEFAULT_SOURCE = (
    Path(settings.BASE_DIR).parent
    / "erpnext"
    / "setup"
    / "setup_wizard"
    / "data"
    / "uom_conversion_data.json"
)
KNOWN_MISSING_UOMS = {"Medio Metro", "Manzana", "Peck"}


class Command(BaseCommand):
    help = "Import ERPNext's global UOM conversion reference data without overwriting edits."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)

    @transaction.atomic
    def handle(self, *args, **options):
        source = options["source"]
        try:
            rows = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Cannot read conversion fixture {source}: {exc}") from exc
        if not isinstance(rows, list):
            raise CommandError("Conversion fixture must be a JSON list")
        if not UnitOfMeasure.objects.exists():
            raise CommandError("Import ERPNext UOMs before conversion factors")

        created = 0
        missing_uoms_created = 0
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise CommandError(f"Conversion row {index} is not an object")
            category_name = row.get("category")
            category = UOMCategory.objects.filter(pk=category_name).first()
            if category is None:
                raise CommandError(f"Conversion row {index} has unknown category {category_name}")

            units = {}
            for field in ("from_uom", "to_uom"):
                name = row.get(field)
                unit = UnitOfMeasure.objects.filter(pk=name).first()
                if unit is None:
                    if name not in KNOWN_MISSING_UOMS:
                        raise CommandError(f"Conversion row {index} has unknown UOM {name}")
                    unit = UnitOfMeasure.objects.create(name=name, category=category)
                    missing_uoms_created += 1
                units[field] = unit

            raw_value = row.get("value")
            if (
                row.get("from_uom") == "Meter"
                and row.get("to_uom") == "Ells (UK)"
                and raw_value == "0.006993s"
            ):
                # The shipped fixture has a single stray trailing character.
                raw_value = "0.006993"
            try:
                value = Decimal(str(raw_value))
            except InvalidOperation as exc:
                raise CommandError(f"Conversion row {index} has invalid value {raw_value}") from exc

            if not UOMConversionFactor.objects.filter(
                from_uom=units["from_uom"], to_uom=units["to_uom"]
            ).exists():
                UOMConversionFactor.objects.create(
                    category=category,
                    from_uom=units["from_uom"],
                    to_uom=units["to_uom"],
                    value=value,
                )
                created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {created} of {len(rows)} conversion factors; "
                f"created {missing_uoms_created} referenced UOMs"
            )
        )
