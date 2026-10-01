import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from demo.product_data import build_product_structure


class Command(BaseCommand):
    help = "Export deterministic, Git-friendly item/BOM master data as UTF-8 JSON."

    def add_arguments(self, parser):
        parser.add_argument("destination", help="Output path, or - to write JSON to stdout.")
        parser.add_argument("--product", help="Export one product and its active BOM closure.")
        parser.add_argument("--include-stock", action="store_true",
                            help="Include current stock as optional opening_stock values.")
        parser.add_argument("--dataset-code", default="product-master",
                            help="Stable dataset identifier stored in the document.")

    def handle(self, *args, **options):
        try:
            payload = build_product_structure(
                product_sku=options.get("product"), include_stock=options["include_stock"],
                dataset_code=options["dataset_code"])
        except ValidationError as exc:
            raise CommandError(" | ".join(exc.messages)) from exc
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if options["destination"] == "-":
            self.stdout.write(content, ending="")
            return
        destination = Path(options["destination"]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")
        self.stdout.write(self.style.SUCCESS(
            f"Product structure exported: {destination} "
            f"({len(payload['items'])} items, {len(payload['boms'])} BOMs)"))
