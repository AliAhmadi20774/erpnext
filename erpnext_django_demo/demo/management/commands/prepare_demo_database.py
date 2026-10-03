from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from demo.database_backup import create_sqlite_backup


class Command(BaseCommand):
    help = "Back up an existing SQLite demo before applying pending migrations, without resetting data."

    def handle(self, *args, **options):
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if not pending:
            self.stdout.write("Database is current; no migration or backup needed.")
            return
        database = settings.DATABASES["default"]
        if database["ENGINE"] == "django.db.backends.sqlite3":
            source = Path(database["NAME"])
            if source.is_file() and source.stat().st_size:
                stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S-%f")
                destination = Path(settings.BASE_DIR) / "backups" / f"before-upgrade-{stamp}.sqlite3"
                try:
                    path = create_sqlite_backup(source, destination)
                except (ValueError, OSError) as exc:
                    raise CommandError(f"Pre-upgrade backup failed; migrations were not applied: {exc}") from exc
                self.stdout.write(f"Verified pre-upgrade backup: {path}")
        call_command("migrate", interactive=False, stdout=self.stdout, stderr=self.stderr)
        self.stdout.write(self.style.SUCCESS("Demo schema upgraded; existing business data preserved."))
