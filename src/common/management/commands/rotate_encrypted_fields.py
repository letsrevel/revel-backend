"""Re-encrypt every encrypted column under the primary SECRET_KEY / SALT_KEY.

Run after deploying a rotation (new key primary, old ones in ``SECRET_KEY_FALLBACKS`` /
``SALT_KEY_FALLBACKS``) so the fallbacks can be dropped later without losing data.

Usage:
    python manage.py rotate_encrypted_fields --dry-run   # counts only
    python manage.py rotate_encrypted_fields             # re-encrypt
    python manage.py rotate_encrypted_fields --verify    # decrypt with the primary key only

Output is ``app.Model.field`` and counts; values and row identifiers are never printed.
"""

import typing as t

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import models, transaction
from django.db.models import F, TextField, Value
from django.db.models.functions import Cast
from encrypted_fields.fields import EncryptedFieldMixin


def encrypted_fields() -> list[tuple[type[models.Model], t.Any]]:
    """Every concrete encrypted field, once (proxies and inherited copies are skipped)."""
    return [
        (model, field)
        for model in apps.get_models()
        if not model._meta.proxy
        for field in model._meta.concrete_fields
        if isinstance(field, EncryptedFieldMixin) and field.model is model
    ]


def _batches(model: type[models.Model], name: str, size: int, *, lock: bool) -> t.Iterator[list[tuple[t.Any, str]]]:
    """Yield ``(pk, raw ciphertext)`` batches in pk order, skipping NULL and empty values."""
    base = (
        model._base_manager.annotate(_raw=Cast(F(name), TextField()))
        .filter(_raw__isnull=False)
        .exclude(_raw="")
        .order_by("pk")
    )
    last = None
    while True:
        qs = base.filter(pk__gt=last) if last is not None else base
        if lock:
            qs = qs.select_for_update()
        with transaction.atomic():
            rows = list(qs.values_list("pk", "_raw")[:size])
            if not rows:
                return
            yield rows
        last = rows[-1][0]


class Command(BaseCommand):
    help = "Re-encrypt encrypted model fields under the primary SECRET_KEY/SALT_KEY (key rotation)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Add CLI arguments."""
        parser.add_argument("--dry-run", action="store_true", help="Count rows that need rotating; write nothing.")
        parser.add_argument(
            "--verify", action="store_true", help="Decrypt every row with ONLY the primary key; fail on any miss."
        )
        parser.add_argument("--batch-size", type=int, default=500, help="Rows per transaction (default: 500).")

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Rotate (or dry-run / verify) every encrypted field."""
        failures = 0
        for model, field in encrypted_fields():
            label = f"{model._meta.label}.{field.name}"
            if options["verify"]:
                failures += self._verify(model, field, label, options["batch_size"])
            else:
                failures += self._rotate(model, field, label, options["batch_size"], dry_run=options["dry_run"])
        if failures:
            raise CommandError(f"{failures} value(s) could not be decrypted with the expected key(s).")

    def _verify(self, model: type[models.Model], field: t.Any, label: str, size: int) -> int:
        primary = Fernet(field.keys[0])
        ok = failed = 0
        for rows in _batches(model, field.name, size, lock=False):
            for _pk, raw in rows:
                try:
                    primary.decrypt(raw.encode())
                    ok += 1
                except InvalidToken:
                    failed += 1
        self.stdout.write(f"{label}: ok={ok} failed={failed}")
        return failed

    def _rotate(self, model: type[models.Model], field: t.Any, label: str, size: int, *, dry_run: bool) -> int:
        if not isinstance(field.f, MultiFernet):
            self.stdout.write(f"{label}: single key configured, nothing to rotate")
            return 0
        primary = Fernet(field.keys[0])
        rotated = current = failed = 0
        for rows in _batches(model, field.name, size, lock=not dry_run):
            updates: list[tuple[t.Any, str]] = []
            for pk, raw in rows:
                try:
                    primary.decrypt(raw.encode())
                    current += 1
                    continue
                except InvalidToken:
                    pass
                try:
                    updates.append((pk, field.f.rotate(raw.encode()).decode()))
                except InvalidToken:
                    failed += 1
            rotated += len(updates)
            if not dry_run:
                for pk, new in updates:
                    # Value(...) bypasses the field's own encryption, which would encrypt a second time.
                    model._base_manager.filter(pk=pk).update(**{field.name: Value(new, output_field=TextField())})
        verb = "would rotate" if dry_run else "rotated"
        self.stdout.write(f"{label}: {verb}={rotated} current={current} failed={failed}")
        return failed
