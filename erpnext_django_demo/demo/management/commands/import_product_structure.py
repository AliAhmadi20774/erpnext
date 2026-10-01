import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from demo.product_data import apply_product_structure


class Command(BaseCommand):
    help = "Validate and atomically import versioned item/BOM master data from JSON."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Path to the versioned product-structure JSON file.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Execute all validations and database operations, then roll back.")
        parser.add_argument("--replace", action="store_true",
                            help="Delete BOM components absent from the imported version.")
        parser.add_argument("--with-opening-stock", action="store_true",
                            help="Record opening stock only for items that have no stock history.")

    def handle(self, *args, **options):
        source = Path(options["source"]).resolve()
        if not source.is_file():
            raise CommandError(f"Product-structure file not found: {source}")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            summary = apply_product_structure(
                payload, replace=options["replace"],
                with_opening_stock=options["with_opening_stock"],
                dry_run=options["dry_run"])
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CommandError(f"Invalid UTF-8 JSON: {exc}") from exc
        except ValidationError as exc:
            raise CommandError(" | ".join(exc.messages)) from exc
        label = "Dry-run passed; database rolled back" if options["dry_run"] else "Import completed"
        self.stdout.write(self.style.SUCCESS(
            f"{label}: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}"))
