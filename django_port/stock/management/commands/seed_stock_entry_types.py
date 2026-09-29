from django.core.management.base import BaseCommand
from django.db import transaction

from stock.models import StockEntryType


STANDARD_TYPES = (
    ("Material Issue", StockEntryType.Purpose.MATERIAL_ISSUE),
    ("Material Receipt", StockEntryType.Purpose.MATERIAL_RECEIPT),
    ("Material Transfer", StockEntryType.Purpose.MATERIAL_TRANSFER),
)


class Command(BaseCommand):
    help = "Create the supported standard ERPNext Stock Entry Types."

    @transaction.atomic
    def handle(self, *args, **options):
        created = 0
        for name, purpose in STANDARD_TYPES:
            entry_type, was_created = StockEntryType.objects.get_or_create(
                name=name,
                defaults={"purpose": purpose, "is_standard": True},
            )
            if not was_created and (
                entry_type.purpose != purpose or not entry_type.is_standard
            ):
                self.stderr.write(
                    self.style.WARNING(
                        f"Skipped {name}: an incompatible record already exists."
                    )
                )
                continue
            created += int(was_created)
        self.stdout.write(
            self.style.SUCCESS(f"Stock Entry Types are ready; created {created}.")
        )
