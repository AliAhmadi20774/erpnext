from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from demo.large_product_demo import ROOT_SKU, ensure_large_product_tree
from demo.models import Item
from demo.product_structure import build_product_tree, product_tree_metrics


class Command(BaseCommand):
    help = "Add the large five-level packaging-line BOM without resetting existing demo data."

    def handle(self, *args, **options):
        try:
            created = ensure_large_product_tree()
            root = Item.objects.get(sku__iexact=ROOT_SKU)
            metrics = product_tree_metrics(build_product_tree(root))
        except ValidationError as exc:
            raise CommandError(" | ".join(exc.messages)) from exc
        label = "Large product tree created" if created else "Existing large product tree preserved"
        self.stdout.write(self.style.SUCCESS(
            f"{label}: {metrics['level_count']} levels, {metrics['component_count']} components; "
            f"/products/tree/{root.pk}/"))
