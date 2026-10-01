"""Replace the Terms and Conditions or the Privacy Policy from a markdown file.

Writes ``common.models.Legal`` (served by ``GET /api/legal``) through the normal model
save, so ``MarkdownField`` sanitization still applies. The diff is computed against
the *sanitized* new text, i.e. exactly what would be stored.

Usage:
    python manage.py update_legal --document terms --file tos.md --dry-run
    python manage.py update_legal --document privacy --file privacy.md
    cat tos.md | python manage.py update_legal --document terms --file -
"""

import difflib
import hashlib
import sys
import typing as t

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from common.fields import sanitize_markdown
from common.models import Legal

DOCUMENT_FIELDS: dict[str, str] = {
    "terms": "terms_and_conditions",
    "privacy": "privacy_policy",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _diff(document: str, current: str, stored: str) -> list[str]:
    return list(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            stored.splitlines(keepends=True),
            fromfile=f"{document} (current)",
            tofile=f"{document} (new)",
        )
    )


class Command(BaseCommand):
    """Update a legal document from a markdown file."""

    help = "Replace the Terms and Conditions or the Privacy Policy from a markdown file (or '-' for stdin)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        parser.add_argument("--document", required=True, choices=sorted(DOCUMENT_FIELDS))
        parser.add_argument("--file", required=True, help="Markdown file path, or '-' to read stdin.")
        parser.add_argument("--dry-run", action="store_true", help="Print a unified diff and change nothing.")

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Execute the command."""
        document: str = options["document"]
        field = DOCUMENT_FIELDS[document]
        new_text = self._read(options["file"])

        stored = sanitize_markdown(new_text)
        if not stored.strip():
            raise CommandError("Refusing to save: the document is empty after sanitization.")
        if stored != new_text:
            self.stderr.write(
                self.style.WARNING("Sanitization changes the input; the diff shows the text as it will be stored.")
            )

        if options["dry_run"]:
            # Non-creating lookup: a dry run must not create the singleton row.
            existing = Legal.objects.filter(pk=Legal.singleton_instance_id).first()
            diff = _diff(document, getattr(existing, field) if existing else "", stored)
            self.stdout.write("".join(diff) if diff else "No changes.")
            self.stdout.write(self.style.WARNING("\nDRY RUN - nothing was saved."))
            return

        Legal.objects.get_or_create(pk=Legal.singleton_instance_id)
        with transaction.atomic():
            # Lock the row so concurrent terms/privacy runs serialize, and read from the DB
            # (not SOLO_CACHE) so the diff and the history record reflect the current state.
            legal = Legal.objects.select_for_update().get(pk=Legal.singleton_instance_id)
            current: str = getattr(legal, field)
            diff = _diff(document, current, stored)
            if not diff:
                self.stdout.write(f"No changes to {document}; nothing saved. sha256={_sha256(current)}")
                return
            setattr(legal, field, new_text)
            # Write only the targeted document; MarkdownField.pre_save still sanitizes it.
            legal.save(update_fields=[field, "updated_at"])

        saved: str = getattr(legal, field)
        self.stdout.write(
            self.style.SUCCESS(
                f"Updated {document} ({len(saved)} chars, {len(diff)} diff lines). sha256={_sha256(saved)}"
            )
        )

    def _read(self, path: str) -> str:
        """Read the markdown from a file or stdin, refusing empty input."""
        if path == "-":
            text = sys.stdin.read()
        else:
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as e:
                raise CommandError(f"Cannot read {path}: {e}") from e
        if not text.strip():
            raise CommandError("Refusing to save an empty document.")
        return text
