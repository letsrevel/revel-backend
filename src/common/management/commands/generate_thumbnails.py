"""Management command to generate thumbnails for existing images.

Usage:
    python manage.py generate_thumbnails                    # All models
    python manage.py generate_thumbnails --dry-run          # Preview only
    python manage.py generate_thumbnails --model events.Organization
    python manage.py generate_thumbnails --field logo
    python manage.py generate_thumbnails --sync             # Run synchronously
    python manage.py generate_thumbnails --force            # Regenerate existing
    python manage.py generate_thumbnails --limit 100        # Process only 100 instances
"""

import typing as t
from dataclasses import dataclass

from django.apps import apps
from django.core.management.base import BaseCommand, CommandParser
from django.db import models

from common.thumbnails.config import (
    THUMBNAIL_CONFIGS,
    ModelThumbnailConfig,
    get_thumbnail_field_names,
)


@dataclass
class ProcessingStats:
    """Statistics for thumbnail processing."""

    processed: int = 0
    skipped: int = 0
    scheduled: int = 0


class Command(BaseCommand):
    """Management command to generate thumbnails for existing images."""

    help = "Generate thumbnails for existing image files"

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command-line arguments."""
        parser.add_argument(
            "--model",
            type=str,
            help="Specific model to process (e.g., 'events.Organization')",
        )
        parser.add_argument(
            "--field",
            type=str,
            help="Specific field to process (e.g., 'logo', 'cover_art', 'profile_picture')",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be processed without actually generating thumbnails",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Run synchronously instead of using Celery tasks",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate thumbnails even if they already exist",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of instances to process (useful for staged rollouts)",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Execute the command."""
        model_filter = options.get("model")
        field_filter = options.get("field")
        dry_run = options.get("dry_run", False)
        sync = options.get("sync", False)
        force = options.get("force", False)
        limit = options.get("limit")

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - no thumbnails will be generated"))
        if limit:
            self.stdout.write(self.style.WARNING(f"LIMIT MODE - processing at most {limit} instances"))

        configs = self._filter_configs(model_filter, field_filter)
        if not configs:
            self.stdout.write(self.style.WARNING("No matching configurations found."))
            return

        stats = ProcessingStats()
        remaining_limit = limit
        for config_key, config in configs:
            remaining_limit = self._process_config(config_key, config, dry_run, sync, force, stats, remaining_limit)
            if remaining_limit is not None and remaining_limit <= 0:
                self.stdout.write(self.style.WARNING("Limit reached, stopping."))
                break

        self._print_summary(dry_run, sync, stats)

    def _filter_configs(
        self,
        model_filter: str | None,
        field_filter: str | None,
    ) -> list[tuple[tuple[str, str, str], ModelThumbnailConfig]]:
        """Filter thumbnail configs based on command options."""
        configs: list[tuple[tuple[str, str, str], ModelThumbnailConfig]] = []
        for config_key, config in THUMBNAIL_CONFIGS.items():
            app_label, model_name, field_name = config_key

            if model_filter:
                model_str = f"{app_label}.{model_name}"
                if model_str.lower() != model_filter.lower():
                    continue

            if field_filter and field_name != field_filter:
                continue

            configs.append((config_key, config))
        return configs

    def _process_config(
        self,
        config_key: tuple[str, str, str],
        config: ModelThumbnailConfig,
        dry_run: bool,
        sync: bool,
        force: bool,
        stats: ProcessingStats,
        limit: int | None,
    ) -> int | None:
        """Process a single model/field configuration.

        Returns:
            Remaining limit after processing (None if no limit was set).
        """
        app_label, model_name, field_name = config_key
        self.stdout.write(f"\nProcessing {app_label}.{model_name}.{field_name}...")

        try:
            model_class = apps.get_model(app_label, model_name)
        except LookupError:
            self.stdout.write(self.style.ERROR(f"  Model {app_label}.{model_name} not found"))
            return limit

        queryset = self._build_queryset(model_class, field_name, config, force)
        count = queryset.count()

        # Apply limit to count if specified
        effective_count = min(count, limit) if limit is not None else count
        limit_msg = f" (limited to {effective_count})" if limit else ""
        self.stdout.write(f"  Found {count} instances to process{limit_msg}")

        if dry_run:
            stats.processed += effective_count
            return limit - effective_count if limit is not None else None

        # Apply limit to queryset
        if limit is not None:
            queryset = queryset[:limit]

        if sync:
            processed = self._process_sync(queryset, model_name, field_name, config, stats)
        else:
            processed = self._process_async(queryset, app_label, model_name, field_name, stats)

        return limit - processed if limit is not None else None

    def _build_queryset(
        self,
        model_class: type[models.Model],
        field_name: str,
        config: ModelThumbnailConfig,
        force: bool,
    ) -> models.QuerySet[models.Model]:
        """Build queryset of instances to process."""
        manager: models.Manager[models.Model] = getattr(model_class, "objects")
        # Filter instances that have a source file (ImageField uses isnull and empty string check)
        queryset = manager.exclude(**{f"{field_name}__isnull": True}).exclude(**{field_name: ""})

        if not force:
            thumbnail_field_names = get_thumbnail_field_names(config)
            if thumbnail_field_names:
                # Filter to only instances that don't have thumbnails yet
                first_thumb_field = thumbnail_field_names[0]
                queryset = queryset.filter(
                    models.Q(**{f"{first_thumb_field}__isnull": True}) | models.Q(**{first_thumb_field: ""})
                )
        return queryset

    def _process_sync(
        self,
        queryset: models.QuerySet[models.Model],
        model_name: str,
        field_name: str,
        config: ModelThumbnailConfig,
        stats: ProcessingStats,
    ) -> int:
        """Process instances synchronously.

        Returns:
            Number of instances processed (including skipped).
        """
        from common.thumbnails.service import generate_and_save_thumbnails

        count = 0
        for instance in queryset.iterator():
            count += 1
            pk = instance.pk
            file_field = getattr(instance, field_name, None)

            if not file_field:
                stats.skipped += 1
                continue

            try:
                original_path = file_field.name
                result = generate_and_save_thumbnails(original_path, config)

                update_fields = []
                for thumb_field_name, path in result.thumbnails.items():
                    if hasattr(instance, thumb_field_name):
                        setattr(instance, thumb_field_name, path)
                        update_fields.append(thumb_field_name)

                if update_fields:
                    instance.save(update_fields=update_fields)

                stats.processed += 1
                if result.has_failures:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  Partial success for {model_name} pk={pk}: "
                            f"{len(result.thumbnails)} generated, {len(result.failures)} failed"
                        )
                    )
                else:
                    self.stdout.write(f"  Generated thumbnails for {model_name} pk={pk}")
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Failed to generate thumbnails for pk={pk}: {e}"))
                stats.skipped += 1
        return count

    def _process_async(
        self,
        queryset: models.QuerySet[models.Model],
        app_label: str,
        model_name: str,
        field_name: str,
        stats: ProcessingStats,
        batch_size: int = 100,
    ) -> int:
        """Schedule thumbnail generation tasks asynchronously with batching.

        Uses Celery groups for efficient task distribution when processing
        large datasets.

        Args:
            queryset: QuerySet of model instances to process.
            app_label: Django app label.
            model_name: Model name.
            field_name: Source field name.
            stats: Processing statistics to update.
            batch_size: Number of tasks per batch (default 100).

        Returns:
            Number of instances processed.
        """
        from celery import group

        from common.thumbnails.tasks import generate_thumbnails_task

        count = 0
        batch: list[t.Any] = []

        for instance in queryset.iterator():
            count += 1
            batch.append(
                generate_thumbnails_task.s(
                    app=app_label,
                    model=model_name,
                    pk=str(instance.pk),
                    field=field_name,
                )
            )
            stats.scheduled += 1

            # Send batch when it reaches batch_size
            if len(batch) >= batch_size:
                group(batch).apply_async()
                batch = []

        # Send any remaining tasks
        if batch:
            group(batch).apply_async()

        self.stdout.write(f"  Scheduled {stats.scheduled} tasks in batches of {batch_size}")
        return count

    def _print_summary(self, dry_run: bool, sync: bool, stats: ProcessingStats) -> None:
        """Print summary of processing."""
        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"DRY RUN: Would process {stats.processed} instances"))
        elif sync:
            self.stdout.write(
                self.style.SUCCESS(f"Generated thumbnails for {stats.processed} instances, skipped {stats.skipped}")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Scheduled {stats.scheduled} thumbnail generation tasks"))
