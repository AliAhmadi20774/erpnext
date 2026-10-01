from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from demo.database_backup import create_sqlite_backup
from demo.models import (AuditEvent, BillOfMaterials, BOMComponent, Customer, FitGapItem,
                         Fulfillment, Invoice, Item, ManagementDecision, Order, OrderLine,
                         JournalEntry, JournalLine, Payment, StockMovement, Supplier)


class Command(BaseCommand):
    help = "Back up the current SQLite database, clear demo business data, and rebuild the presentation dataset."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Confirm deletion of current demo business data.")
        parser.add_argument("--no-backup", action="store_true", help="Skip the automatic pre-reset backup.")

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("Reset deletes current demo business data; rerun with --yes.")
        if not options["no_backup"]:
            if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3":
                raise CommandError("Automatic reset backup currently supports SQLite only.")
            stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S-%f")
            destination = Path(settings.BASE_DIR) / "backups" / f"before-reset-{stamp}.sqlite3"
            path = create_sqlite_backup(settings.DATABASES["default"]["NAME"], destination)
            self.stdout.write(f"Pre-reset backup: {path}")

        with transaction.atomic():
            # Management decisions are governance records, not disposable presentation data.
            # Keep both the records and their audit trail across demo resets.
            governance_types = [ManagementDecision._meta.model_name, FitGapItem._meta.model_name]
            AuditEvent.objects.exclude(object_type__in=governance_types).delete()
            JournalLine.objects.all().delete()
            JournalEntry.objects.all().delete()
            Payment.objects.all().delete()
            Invoice.objects.all().delete()
            StockMovement.objects.all().delete()
            Fulfillment.objects.all().delete()
            OrderLine.objects.all().delete()
            Order.objects.all().delete()
            Customer.objects.all().delete()
            Supplier.objects.all().delete()
            BOMComponent.objects.all().delete()
            BillOfMaterials.objects.all().delete()
            Item.objects.all().delete()
        call_command("seed_demo", stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS("Presentation data reset and verified."))
