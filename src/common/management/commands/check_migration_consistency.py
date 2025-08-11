"""Check migration consistency in case of rollbacks."""

import importlib
import typing as t
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Fails if DB has applied migrations that are missing from the codebase (handles squashed migrations)"

    def handle(self, *args: t.Any, **options: t.Any) -> None:  # noqa: C901
        """Check if all applied migrations in the database exist in the codebase (in case of rollbacks)."""
        cursor = connection.cursor()
        cursor.execute("SELECT app, name FROM django_migrations")
        applied = defaultdict(set)
        for app, name in cursor.fetchall():
            applied[app].add(name)

        # Collect all replaced migration names (from squashed files)
        replaced_migrations = defaultdict(set)

        for app in applied.keys():
            try:
                app_module = importlib.import_module(app)
                migrations_path = Path(app_module.__path__[0]) / "migrations"
            except (ModuleNotFoundError, AttributeError):
                continue

            for file in migrations_path.glob("[0-9]*_*.py"):
                module_name = f"{app}.migrations.{file.stem}"
                try:
                    mod = importlib.import_module(module_name)
                    replaces = getattr(mod.Migration, "replaces", [])
                    for replaced_app, replaced_name in replaces:
                        replaced_migrations[replaced_app].add(replaced_name)
                except Exception:
                    continue  # ignore broken or invalid migration files

        missing = []

        for app, names in applied.items():
            try:
                migrations_path = Path(importlib.import_module(app).__path__[0]) / "migrations"
            except (ModuleNotFoundError, AttributeError):
                continue

            code_migrations = {f.stem for f in migrations_path.glob("[0-9]*_*.py")} | replaced_migrations.get(
                app, set()
            )

            for name in names:
                if name not in code_migrations:
                    missing.append((app, name))

        if missing:
            self.stderr.write(self.style.ERROR("❌ Detected migrations in DB missing from code (not squashed):"))
            for app, name in missing:
                self.stderr.write(f"  - {app}: {name}")
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("✅ All DB-applied migrations exist in code or are covered by squash."))
