from django.core.management.base import BaseCommand

from catalog.models import ItemGroup


class Command(BaseCommand):
    help = "Create ERPNext's initial All Item Groups and Default groups when absent."

    def handle(self, *args, **options):
        root = ItemGroup.objects.filter(parent_item_group__isnull=True).first()
        if root is None:
            root = ItemGroup.objects.create(name="All Item Groups", is_group=True)
        _, created = ItemGroup.objects.get_or_create(
            name="Default", defaults={"parent_item_group": root, "is_group": False}
        )
        self.stdout.write(
            self.style.SUCCESS(f"Root: {root.name}; Default created: {created}")
        )
