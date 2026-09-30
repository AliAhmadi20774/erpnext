from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from demo.database_backup import create_sqlite_backup


class Command(BaseCommand):
    help = "Create a consistent SQLite backup of the complete demo, including users and audit events."

    def add_arguments(self, parser):
        parser.add_argument("output", nargs="?")
        parser.add_argument("--overwrite", action="store_true")

    def handle(self, *args, **options):
        if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3":
            raise CommandError("backup_demo currently supports SQLite only.")
        output = options["output"] or str(
            Path(settings.BASE_DIR) / "backups" / f"erp-demo-{timezone.now():%Y%m%d-%H%M%S}.sqlite3"
        )
        try:
            path = create_sqlite_backup(settings.DATABASES["default"]["NAME"], output,
                                        overwrite=options["overwrite"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Verified backup created: {path}"))
