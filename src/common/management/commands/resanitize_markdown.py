"""Management command to re-sanitize all MarkdownField content in the database.

Usage:
    python manage.py resanitize_markdown
    python manage.py resanitize_markdown --dry-run
    python manage.py resanitize_markdown --model events.Event
"""

import typing as t

from django.apps import apps
from django.core.management.base import BaseCommand, CommandParser
from django.db import models

from common.fields import get_markdown_field_registry, sanitize_markdown


class Command(BaseCommand):
    """Re-sanitize all MarkdownField content in the database."""

    help = "Re-sanitize all MarkdownField content using the current sanitization rules"

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be changed without making changes",
        )
        parser.add_argument(
            "--model",
            type=str,
            help="Only process a specific model (format: app_label.ModelName)",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show before/after for each changed field",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Execute the command."""
        dry_run = options["dry_run"]
        verbose = options["verbose"]

        registry = self._get_registry(options.get("model"))
        if not registry:
            return

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - No changes will be made\n"))

        total_processed = 0
        total_changed = 0

        for model_class, field_names in registry.items():
            processed, changed = self._process_model(model_class, field_names, dry_run, verbose)
            total_processed += processed
            total_changed += changed

        self._print_summary(total_processed, total_changed, dry_run)

    def _get_registry(self, target_model: str | None) -> dict[type[models.Model], list[str]]:
        """Get the registry, optionally filtered to a specific model."""
        registry = get_markdown_field_registry()

        if not registry:
            self.stdout.write(self.style.WARNING("No models with MarkdownField found"))
            return {}

        if not target_model:
            return registry

        try:
            app_label, model_name = target_model.split(".")
            model_class = apps.get_model(app_label, model_name)
        except (ValueError, LookupError) as e:
            self.stderr.write(self.style.ERROR(f"Invalid model: {target_model} - {e}"))
            return {}

        if model_class not in registry:
            self.stderr.write(self.style.ERROR(f"{target_model} has no MarkdownFields"))
            return {}

        return {model_class: registry[model_class]}

    def _process_model(
        self,
        model_class: type[models.Model],
        field_names: list[str],
        dry_run: bool,
        verbose: bool,
    ) -> tuple[int, int]:
        """Process all instances of a model."""
        label = f"{model_class._meta.app_label}.{model_class.__name__}"
        self.stdout.write(f"\n{label} ({', '.join(field_names)})")

        processed = 0
        changed = 0

        for instance in model_class.objects.all():  # type: ignore[attr-defined]
            processed += 1
            changes = self._sanitize_instance(instance, field_names)

            if not changes:
                continue

            changed += 1
            if verbose:
                self._print_changes(instance, changes)
            if not dry_run:
                instance.save(update_fields=list(changes.keys()))

        self.stdout.write(f"  {processed} processed, {changed} changed")
        return processed, changed

    def _sanitize_instance(
        self,
        instance: models.Model,
        field_names: list[str],
    ) -> dict[str, tuple[str, str]]:
        """Sanitize fields and return dict of changes: {field: (old, new)}."""
        changes: dict[str, tuple[str, str]] = {}

        for field_name in field_names:
            old_value = getattr(instance, field_name)
            if old_value is None:
                continue

            new_value = sanitize_markdown(old_value)
            if old_value != new_value:
                changes[field_name] = (old_value, new_value)
                setattr(instance, field_name, new_value)

        return changes

    def _print_changes(
        self,
        instance: models.Model,
        changes: dict[str, tuple[str, str]],
    ) -> None:
        """Print changes for an instance."""
        for field_name, (old, new) in changes.items():
            self.stdout.write(f"    {instance._meta.model.__name__}(pk={instance.pk}).{field_name}")
            self.stdout.write(f"      OLD: {self._truncate(old)}")
            self.stdout.write(f"      NEW: {self._truncate(new)}")

    def _truncate(self, value: str, max_len: int = 80) -> str:
        """Truncate string for display."""
        value = value.replace("\n", "\\n")
        return value[:max_len] + "..." if len(value) > max_len else value

    def _print_summary(self, processed: int, changed: int, dry_run: bool) -> None:
        """Print final summary."""
        action = "Would change" if dry_run else "Changed"
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"{action} {changed}/{processed} records"))
