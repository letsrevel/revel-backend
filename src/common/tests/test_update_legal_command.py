"""Tests for the ``update_legal`` management command."""

import difflib
import hashlib
import io
import typing as t
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command

from common.models import Legal

pytestmark = pytest.mark.django_db


def _run(*args: str, stdin: str | None = None, monkeypatch: pytest.MonkeyPatch | None = None) -> str:
    if stdin is not None:
        assert monkeypatch is not None
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    out = io.StringIO()
    call_command("update_legal", *args, stdout=out, stderr=io.StringIO())
    return out.getvalue()


@pytest.fixture
def legal() -> Legal:
    obj = Legal.get_solo()
    obj.terms_and_conditions = "# Terms\n\nOld clause.\n"
    obj.privacy_policy = "# Privacy\n\nOld privacy.\n"
    obj.save()
    return obj


@pytest.fixture
def md_file(tmp_path: Path) -> Path:
    path = tmp_path / "tos.md"
    path.write_text("# Terms\n\nNew clause.\n", encoding="utf-8")
    return path


def test_dry_run_prints_diff_and_saves_nothing(legal: Legal, md_file: Path) -> None:
    out = _run("--document", "terms", "--file", str(md_file), "--dry-run")

    assert "-Old clause." in out
    assert "+New clause." in out
    assert "DRY RUN" in out
    legal.refresh_from_db()
    assert legal.terms_and_conditions == "# Terms\n\nOld clause.\n"


def test_updates_terms_and_prints_hash(legal: Legal, md_file: Path) -> None:
    out = _run("--document", "terms", "--file", str(md_file))

    legal.refresh_from_db()
    assert legal.terms_and_conditions == "# Terms\n\nNew clause.\n"
    assert legal.privacy_policy == "# Privacy\n\nOld privacy.\n"
    assert hashlib.sha256(legal.terms_and_conditions.encode()).hexdigest() in out
    # The public endpoint serves the new text (SOLO_CACHE refreshed on save).
    assert Legal.get_solo().terms_and_conditions == "# Terms\n\nNew clause.\n"


def test_updates_privacy_from_stdin(legal: Legal, monkeypatch: pytest.MonkeyPatch) -> None:
    _run("--document", "privacy", "--file", "-", stdin="# Privacy\n\nNew privacy.\n", monkeypatch=monkeypatch)

    legal.refresh_from_db()
    assert legal.privacy_policy == "# Privacy\n\nNew privacy.\n"
    assert legal.terms_and_conditions == "# Terms\n\nOld clause.\n"


def test_save_goes_through_sanitization(legal: Legal, tmp_path: Path) -> None:
    path = tmp_path / "evil.md"
    path.write_text("# Terms\n\n<script>alert(1)</script>Safe text.\n", encoding="utf-8")

    _run("--document", "terms", "--file", str(path))

    legal.refresh_from_db()
    assert "<script>" not in legal.terms_and_conditions
    assert "Safe text." in legal.terms_and_conditions


def test_dry_run_diff_shows_sanitized_text(legal: Legal, tmp_path: Path) -> None:
    path = tmp_path / "evil.md"
    path.write_text("<script>alert(1)</script>Safe text.\n", encoding="utf-8")

    out = _run("--document", "terms", "--file", str(path), "--dry-run")

    assert "<script>" not in out
    assert "+Safe text." in out


@pytest.mark.parametrize("content", ["", "   \n\n"])
def test_refuses_empty_input(legal: Legal, tmp_path: Path, content: str) -> None:
    path = tmp_path / "empty.md"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(CommandError, match="empty"):
        _run("--document", "terms", "--file", str(path))

    legal.refresh_from_db()
    assert legal.terms_and_conditions == "# Terms\n\nOld clause.\n"


def test_missing_file_is_a_command_error(legal: Legal, tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="Cannot read"):
        _run("--document", "terms", "--file", str(tmp_path / "nope.md"))


def test_unchanged_document_is_not_saved(legal: Legal, tmp_path: Path) -> None:
    path = tmp_path / "same.md"
    path.write_text(legal.terms_and_conditions, encoding="utf-8")
    before = legal.updated_at

    out = _run("--document", "terms", "--file", str(path))

    assert "No changes" in out
    legal.refresh_from_db()
    assert legal.updated_at == before


def test_invalid_document_choice_rejected(md_file: Path) -> None:
    with pytest.raises(CommandError):
        _run("--document", "cookies", "--file", str(md_file))


def test_dry_run_without_legal_row_creates_nothing(md_file: Path) -> None:
    Legal.objects.all().delete()

    out = _run("--document", "terms", "--file", str(md_file), "--dry-run")

    assert "+New clause." in out
    assert "DRY RUN" in out
    assert not Legal.objects.exists()


def test_input_empty_after_sanitization_is_refused(legal: Legal, tmp_path: Path) -> None:
    path = tmp_path / "evil.md"
    path.write_text("<script>alert(1)</script>\n", encoding="utf-8")

    with pytest.raises(CommandError, match="empty after sanitization"):
        _run("--document", "terms", "--file", str(path))

    legal.refresh_from_db()
    assert legal.terms_and_conditions == "# Terms\n\nOld clause.\n"


@pytest.mark.parametrize(
    ("document", "field", "other_field"),
    [("terms", "terms_and_conditions", "privacy_policy"), ("privacy", "privacy_policy", "terms_and_conditions")],
)
def test_update_writes_only_the_targeted_document(
    legal: Legal, tmp_path: Path, document: str, field: str, other_field: str
) -> None:
    """A concurrent write to the other document, landing after our read, survives our save."""
    path = tmp_path / "new.md"
    path.write_text("# New\n\nNew text.\n", encoding="utf-8")
    real_diff = difflib.unified_diff

    def diff_then_concurrent_write(*args: t.Any, **kwargs: t.Any) -> t.Iterator[str]:
        Legal.objects.update(**{other_field: "Concurrent edit."})
        return real_diff(*args, **kwargs)

    with patch("difflib.unified_diff", side_effect=diff_then_concurrent_write):
        _run("--document", document, "--file", str(path))

    legal.refresh_from_db()
    assert getattr(legal, field) == "# New\n\nNew text.\n"
    assert getattr(legal, other_field) == "Concurrent edit."
