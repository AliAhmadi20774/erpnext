import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import UOMCategory, UnitOfMeasure


DEFAULT_SOURCE = (
    Path(settings.BASE_DIR).parent
    / "erpnext"
    / "setup"
    / "setup_wizard"
    / "data"
    / "uom_data.json"
)


class Command(BaseCommand):
    help = "Import ERPNext's setup-wizard UOM reference data without overwriting existing units."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)

    @transaction.atomic
    def handle(self, *args, **options):
        source = options["source"]
        try:
            rows = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Cannot read UOM fixture {source}: {exc}") from exc

        if not isinstance(rows, list):
            raise CommandError("UOM fixture must be a JSON list")

        created = 0
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("uom_name"), str):
                raise CommandError("Every UOM row must contain a string uom_name")
            name = row["uom_name"].strip()
            if not name:
                raise CommandError("UOM name cannot be empty")

            category = None
            if row.get("category"):
                category, _ = UOMCategory.objects.get_or_create(name=row["category"])

            _, was_created = UnitOfMeasure.objects.get_or_create(
                name=name,
                defaults={
                    "category": category,
                    "symbol": row.get("symbol") or "",
                    "common_code": row.get("common_code") or "",
                    "description": row.get("description") or "",
                    "enabled": bool(row.get("enabled", 1)),
                    "must_be_whole_number": bool(row.get("must_be_whole_number", 0)),
                },
            )
            created += was_created

        self.stdout.write(self.style.SUCCESS(f"Created {created} of {len(rows)} UOMs"))
