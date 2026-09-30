from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from demo.database_backup import restore_sqlite_backup


class Command(BaseCommand):
    help = "Validate and restore a complete SQLite demo backup. Stop the web server before running."

    def add_arguments(self, parser):
        parser.add_argument("backup")
        parser.add_argument("--yes", action="store_true", help="Confirm replacement of the active database.")

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("Restore replaces the active database; rerun with --yes after stopping the server.")
        if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3":
            raise CommandError("restore_demo currently supports SQLite only.")
        connections.close_all()
        try:
            path = restore_sqlite_backup(options["backup"], settings.DATABASES["default"]["NAME"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Verified backup restored to: {path}"))
