"""Tests for accounts.service.referral_application_service."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from ninja.errors import HttpError

from accounts.exceptions import ReferralApplicationConflictError, ReferralApplicationsDisabledError
from accounts.models import ReferralApplication, ReferralCode
from accounts.service import referral_application_service as svc
from common.models import SiteSettings

pytestmark = pytest.mark.django_db

PENDING = ReferralApplication.Status.PENDING
APPROVED = ReferralApplication.Status.APPROVED
REJECTED = ReferralApplication.Status.REJECTED
BLOCKED = ReferralApplication.Status.BLOCKED


@pytest.fixture
def applications_enabled() -> t.Iterator[None]:
    site = SiteSettings.get_solo()
    site.referral_applications_enabled = True
    site.save()
    yield
    site.referral_applications_enabled = False
    site.save()


@pytest.fixture
def mock_email() -> t.Iterator[MagicMock]:
    with patch("accounts.tasks.send_account_email.delay") as m:
        yield m


@pytest.fixture
def mock_pushover() -> t.Iterator[MagicMock]:
    with patch("accounts.tasks.notify_admin_new_referral_application.delay") as m:
        yield m


class TestSanitizeNote:
    def test_strips_tags_and_whitespace(self) -> None:
        assert svc.sanitize_note("  <b>hi</b> <script>x</script> there ") == "hi x there"


class TestSubmitApplication:
    def test_disabled_raises_404_error(self) -> None:
        with pytest.raises(ReferralApplicationsDisabledError):
            svc.submit_application(email="a@example.com", code="abc", note="hi")

    def test_creates_pending_row_and_dispatches(
        self,
        applications_enabled: None,
        mock_email: MagicMock,
        mock_pushover: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            app = svc.submit_application(email="A@Example.com", code="mycode", note="<p>I run events</p>")

        assert app is not None
        assert app.status == PENDING and app.source == ReferralApplication.Source.APPLICATION
        assert app.email == "a@example.com" and app.note == "I run events"
        mock_email.assert_called_once()
        assert mock_email.call_args.args[0] == "referral_application_received"
        assert mock_email.call_args.args[1] == "a@example.com"
        mock_pushover.assert_called_once_with(application_id=str(app.id))

    def test_empty_note_after_sanitizing_is_422(self, applications_enabled: None) -> None:
        with pytest.raises(HttpError) as exc:
            svc.submit_application(email="a@example.com", code="abc", note="<b></b>")
        assert exc.value.status_code == 422

    def test_pending_duplicate_is_409(
        self, applications_enabled: None, mock_email: MagicMock, mock_pushover: MagicMock
    ) -> None:
        svc.submit_application(email="a@example.com", code="one", note="hi")
        with pytest.raises(ReferralApplicationConflictError):
            svc.submit_application(email="a+tag@example.com", code="two", note="hi")

    def test_taken_code_is_409(self, applications_enabled: None, revel_user_factory: t.Any) -> None:
        ReferralCode.objects.create(user=revel_user_factory(), code="Taken")
        with pytest.raises(ReferralApplicationConflictError):
            svc.submit_application(email="a@example.com", code="taken", note="hi")

    def test_code_held_by_live_application_is_409(
        self, applications_enabled: None, mock_email: MagicMock, mock_pushover: MagicMock
    ) -> None:
        svc.submit_application(email="a@example.com", code="held", note="hi")
        with pytest.raises(ReferralApplicationConflictError):
            svc.submit_application(email="b@example.com", code="HELD", note="hi")

    def test_code_from_rejected_application_is_reusable(
        self, applications_enabled: None, mock_email: MagicMock, mock_pushover: MagicMock
    ) -> None:
        ReferralApplication.objects.create(email="x@example.com", code="free", note="hi", status=REJECTED)
        assert svc.submit_application(email="a@example.com", code="free", note="hi") is not None

    def test_blocked_email_is_silently_dropped(
        self, applications_enabled: None, mock_email: MagicMock, mock_pushover: MagicMock
    ) -> None:
        ReferralApplication.objects.create(email="a@example.com", code="old", note="hi", status=BLOCKED)
        assert svc.submit_application(email="A+again@example.com", code="new", note="hi") is None
        assert ReferralApplication.objects.filter(status=PENDING).count() == 0
        mock_email.assert_not_called()

    def test_already_enrolled_email_is_silently_dropped(
        self, applications_enabled: None, revel_user_factory: t.Any, mock_email: MagicMock
    ) -> None:
        user = revel_user_factory(email="a@example.com")
        ReferralCode.objects.create(user=user, code="have")
        assert svc.submit_application(email="a@example.com", code="new", note="hi") is None
        mock_email.assert_not_called()
