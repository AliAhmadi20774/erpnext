import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


REQUIRED_TABLES = {"django_migrations", "auth_user", "demo_item", "demo_order"}


def validate_sqlite_database(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"Backup file does not exist: {path}")
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise ValueError(f"SQLite integrity check failed: {result}")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
    except sqlite3.DatabaseError as exc:
        raise ValueError("The selected file is not a valid SQLite database.") from exc
    missing = REQUIRED_TABLES - tables
    if missing:
        raise ValueError(f"Backup is missing required tables: {', '.join(sorted(missing))}")
    return path


def create_sqlite_backup(source, destination, *, overwrite=False):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        raise ValueError("Backup destination must be different from the active database.")
    if destination.exists() and not overwrite:
        raise ValueError("Backup destination already exists; use --overwrite to replace it.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".sqlite3", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with closing(sqlite3.connect(source)) as source_db, closing(sqlite3.connect(temporary)) as backup_db:
            source_db.backup(backup_db)
        validate_sqlite_database(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def restore_sqlite_backup(source, destination):
    source = validate_sqlite_database(source)
    destination = Path(destination).resolve()
    if source == destination:
        raise ValueError("Backup source and active database cannot be the same file.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".sqlite3", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copy2(source, temporary)
        validate_sqlite_database(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
