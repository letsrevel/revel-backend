"""Tests for the ``send_system_announcement`` management command and its service."""

import io
import typing as t
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import CommandError, call_command

from accounts.models import RevelUser
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service import system_announcement

pytestmark = pytest.mark.django_db

DISPATCH = "notifications.service.system_announcement.dispatch_notifications_batch"


@pytest.fixture
def body_file(tmp_path: Path) -> Path:
    path = tmp_path / "body.html"
    path.write_text(
        '<p>First paragraph.</p><p>See <a href="https://example.com/x">this</a>.</p><script>x()</script>',
        encoding="utf-8",
    )
    return path


def _run(*args: str) -> str:
    out = io.StringIO()
    call_command("send_system_announcement", *args, stdout=out, stderr=io.StringIO())
    return out.getvalue()


def _announcements() -> t.Any:
    return Notification.objects.filter(notification_type=NotificationType.SYSTEM_ANNOUNCEMENT)


@patch(DISPATCH)
def test_dry_run_reports_count_and_renders_email_without_creating(
    mock_dispatch: MagicMock, regular_user: RevelUser, guest_user: RevelUser, body_file: Path
) -> None:
    out = _run("--title", "Policy update", "--body-file", str(body_file), "--dry-run")

    assert "Recipients (ALL matching users): 1" in out
    assert "Subject: Revel - Policy update" in out
    assert "First paragraph." in out
    assert "x()" not in out  # script stripped by sanitize_html
    assert "DRY RUN" in out
    assert not _announcements().exists()
    mock_dispatch.delay.assert_not_called()


@patch(DISPATCH)
def test_broadcast_without_confirm_is_refused(
    mock_dispatch: MagicMock, regular_user: RevelUser, body_file: Path
) -> None:
    with pytest.raises(CommandError, match="--confirm"):
        _run("--title", "T", "--body-file", str(body_file))

    assert not _announcements().exists()
    mock_dispatch.delay.assert_not_called()


@patch(DISPATCH)
def test_broadcast_with_confirm_sends_to_active_non_guests(
    mock_dispatch: MagicMock,
    regular_user: RevelUser,
    guest_user: RevelUser,
    django_user_model: type[RevelUser],
    body_file: Path,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    inactive = django_user_model.objects.create_user(username="gone@example.com", email="gone@example.com")
    inactive.is_active = False
    inactive.save()

    with django_capture_on_commit_callbacks(execute=True):
        out = _run("--title", "T", "--body-file", str(body_file), "--url", "https://example.com/p", "--confirm")

    assert "queued for 1 users" in out
    notif = _announcements().get()
    assert notif.user == regular_user
    assert notif.context["announcement_title"] == "T"
    assert "<script>" not in notif.context["announcement_body"]
    assert notif.context["policy_url"] == "https://example.com/p"
    mock_dispatch.delay.assert_called_once_with([str(notif.id)])


@patch(DISPATCH)
def test_include_guests(
    mock_dispatch: MagicMock, regular_user: RevelUser, guest_user: RevelUser, body_file: Path
) -> None:
    _run("--title", "T", "--body-file", str(body_file), "--include-guests", "--confirm")

    assert set(_announcements().values_list("user_id", flat=True)) == {regular_user.id, guest_user.id}


@patch(DISPATCH)
def test_to_email_sends_only_to_named_users(
    mock_dispatch: MagicMock,
    regular_user: RevelUser,
    guest_user: RevelUser,
    body_file: Path,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        out = _run("--title", "T", "--body-file", str(body_file), "--to-email", "REGULAR@example.com")

    assert "Recipients (the given addresses): 1" in out
    assert list(_announcements().values_list("user_id", flat=True)) == [regular_user.id]
    mock_dispatch.delay.assert_called_once()


@patch(DISPATCH)
def test_to_email_is_repeatable_and_includes_named_guests(
    mock_dispatch: MagicMock, regular_user: RevelUser, guest_user: RevelUser, body_file: Path
) -> None:
    _run(
        "--title",
        "T",
        "--body-file",
        str(body_file),
        "--to-email",
        regular_user.email,
        "--to-email",
        guest_user.email,
    )

    assert set(_announcements().values_list("user_id", flat=True)) == {regular_user.id, guest_user.id}


@patch(DISPATCH)
def test_to_email_unknown_address_fails_before_sending(
    mock_dispatch: MagicMock, regular_user: RevelUser, body_file: Path
) -> None:
    with pytest.raises(CommandError, match="nobody@example.com"):
        _run(
            "--title",
            "T",
            "--body-file",
            str(body_file),
            "--to-email",
            regular_user.email,
            "--to-email",
            "nobody@example.com",
        )

    assert not _announcements().exists()


@patch(DISPATCH)
def test_body_from_stdin(mock_dispatch: MagicMock, regular_user: RevelUser, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("<p>From stdin</p>"))

    _run("--title", "T", "--body-file", "-", "--to-email", regular_user.email)

    assert _announcements().get().context["announcement_body"] == "<p>From stdin</p>"


@pytest.mark.parametrize("body", ["", "  \n", "<script>only()</script>"])
def test_empty_body_refused(regular_user: RevelUser, tmp_path: Path, body: str) -> None:
    path = tmp_path / "b.html"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(CommandError, match="empty"):
        _run("--title", "T", "--body-file", str(path), "--to-email", regular_user.email)


def test_title_validation(regular_user: RevelUser, body_file: Path) -> None:
    with pytest.raises(CommandError, match="empty"):
        _run("--title", "  ", "--body-file", str(body_file), "--dry-run")
    with pytest.raises(CommandError, match="200"):
        _run("--title", "x" * 201, "--body-file", str(body_file), "--dry-run")


def test_missing_body_file(body_file: Path, tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="Cannot read"):
        _run("--title", "T", "--body-file", str(tmp_path / "missing.html"), "--dry-run")


@patch(DISPATCH)
def test_no_recipients_sends_nothing(
    mock_dispatch: MagicMock, django_user_model: type[RevelUser], body_file: Path
) -> None:
    django_user_model.objects.update(is_active=False)

    out = _run("--title", "T", "--body-file", str(body_file), "--confirm")

    assert "nothing sent" in out
    assert not _announcements().exists()


@patch(DISPATCH)
def test_send_batches_dispatch_per_batch(
    mock_dispatch: MagicMock,
    django_user_model: type[RevelUser],
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Each batch gets its own dispatch with its own ids (no loop-variable capture)."""
    monkeypatch.setattr(system_announcement, "BATCH_SIZE", 2)
    for i in range(5):
        django_user_model.objects.create_user(username=f"u{i}@example.com", email=f"u{i}@example.com")
    context = system_announcement.build_context("T", "<p>B</p>")

    with django_capture_on_commit_callbacks(execute=True):
        sent = system_announcement.send(context, system_announcement.get_recipients())

    assert sent == 5
    dispatched = [call.args[0] for call in mock_dispatch.delay.call_args_list]
    assert [len(batch) for batch in dispatched] == [2, 2, 1]
    assert len({nid for batch in dispatched for nid in batch}) == 5


def test_email_preview_text_part_is_plain_text(regular_user: RevelUser) -> None:
    """The text/plain part carries no HTML escaping and keeps link URLs."""
    context = system_announcement.build_context(
        "Q&A: what's new",
        '<p>Tom &amp; Jerry</p><p>Read <a href="https://example.com/x">this</a>.</p>',
        "https://example.com/more",
    )

    preview = system_announcement.render_email_preview(context, regular_user)

    assert preview.subject == "Revel - Q&A: what's new"
    assert preview.text_body.startswith("Q&A: what's new\nRevel\n\nTom & Jerry\n\nRead this (https://example.com/x).")
    assert "&amp;" not in preview.text_body
    assert "&#x27;" not in preview.text_body
    assert "https://example.com/more" in preview.text_body
