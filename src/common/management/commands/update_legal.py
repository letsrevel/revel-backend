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

from common.fields import sanitize_markdown
from common.models import Legal

DOCUMENT_FIELDS: dict[str, str] = {
    "terms": "terms_and_conditions",
    "privacy": "privacy_policy",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

        legal = Legal.get_solo()
        legal.refresh_from_db()  # get_solo() may be served from SOLO_CACHE; diff against the DB
        current: str = getattr(legal, field)
        stored = sanitize_markdown(new_text)

        if stored != new_text:
            self.stderr.write(
                self.style.WARNING("Sanitization changes the input; the diff shows the text as it will be stored.")
            )

        diff = list(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                stored.splitlines(keepends=True),
                fromfile=f"{document} (current)",
                tofile=f"{document} (new)",
            )
        )

        if options["dry_run"]:
            self.stdout.write("".join(diff) if diff else "No changes.")
            self.stdout.write(self.style.WARNING("\nDRY RUN - nothing was saved."))
            return

        if not diff:
            self.stdout.write(f"No changes to {document}; nothing saved. sha256={_sha256(current)}")
            return

        setattr(legal, field, new_text)
        legal.save()
        legal.refresh_from_db()
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
