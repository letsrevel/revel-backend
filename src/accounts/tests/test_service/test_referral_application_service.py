"""Tests for accounts.service.referral_application_service."""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from ninja.errors import HttpError

from accounts.exceptions import (
    ReferralAlreadyActiveError,
    ReferralApplicationConflictError,
    ReferralApplicationsDisabledError,
)
from accounts.models import ReferralApplication, ReferralCode, RevelUser
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
        assert mock_email.call_args.kwargs["context"] == {"code": "mycode"}
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

    def test_same_code_resubmission_reports_pending_duplicate(
        self, applications_enabled: None, mock_email: MagicMock, mock_pushover: MagicMock
    ) -> None:
        svc.submit_application(email="a@example.com", code="one", note="hi")
        with pytest.raises(ReferralApplicationConflictError) as exc:
            svc.submit_application(email="a+tag@example.com", code="one", note="hi")
        assert str(exc.value) == "You already have a pending application."

    def test_taken_code_is_409(self, applications_enabled: None, revel_user_factory: t.Any) -> None:
        ReferralCode.objects.create(user=revel_user_factory(), code="Taken")
        with pytest.raises(ReferralApplicationConflictError):
            svc.submit_application(email="a@example.com", code="taken", note="hi")

    def test_code_held_by_approved_application_is_409(self, applications_enabled: None) -> None:
        ReferralApplication.objects.create(email="x@example.com", code="reserved", note="hi", status=APPROVED)
        with pytest.raises(ReferralApplicationConflictError):
            svc.submit_application(email="a@example.com", code="RESERVED", note="hi")

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
        assert not ReferralApplication.objects.exists()

    def test_enrolled_email_asking_for_a_taken_code_gets_the_same_409_as_anyone(
        self, applications_enabled: None, revel_user_factory: t.Any, mock_email: MagicMock
    ) -> None:
        """The code check runs first, so the response cannot reveal that the email is enrolled."""
        ReferralCode.objects.create(user=revel_user_factory(email="other@example.com"), code="taken")
        ReferralCode.objects.create(user=revel_user_factory(email="a@example.com"), code="have")
        with pytest.raises(ReferralApplicationConflictError) as exc:
            svc.submit_application(email="a@example.com", code="TAKEN", note="hi")
        assert str(exc.value) == "This referral code is already taken."


@pytest.fixture
def admin(revel_user_factory: t.Any) -> RevelUser:
    return t.cast(RevelUser, revel_user_factory(is_staff=True, is_superuser=True))


@pytest.fixture
def pending(mock_email: MagicMock) -> ReferralApplication:
    return ReferralApplication.objects.create(email="new@example.com", code="newcode", note="hi")


class TestApprove:
    def test_no_account_sends_invite_and_stays_unenrolled(
        self,
        pending: ReferralApplication,
        admin: RevelUser,
        mock_email: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            svc.approve(pending, actor=admin)

        pending.refresh_from_db()
        assert pending.status == APPROVED and pending.decided_by == admin and pending.decided_at is not None
        assert pending.user is None
        args, kwargs = mock_email.call_args
        assert args == ("referral_invite", "new@example.com")
        assert kwargs["token"] == str(pending.id)
        assert kwargs["context"] == {"code": "newcode", "revenue_share_percent": "15.00", "admin_note": ""}

    def test_existing_account_gets_code_and_enrolled_email(
        self,
        pending: ReferralApplication,
        admin: RevelUser,
        revel_user_factory: t.Any,
        mock_email: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        user = revel_user_factory(email="new@example.com")
        pending.revenue_share_percent = Decimal("20.00")
        pending.save()

        with django_capture_on_commit_callbacks(execute=True):
            svc.approve(pending, actor=admin)

        code = ReferralCode.objects.get(user=user)
        assert code.code == "newcode" and code.revenue_share_percent == Decimal("20.00") and code.is_active
        pending.refresh_from_db()
        assert pending.user == user
        assert mock_email.call_args.args == ("referral_enrolled", "new@example.com")

    def test_existing_inactive_code_is_reactivated_keeping_its_code(
        self,
        pending: ReferralApplication,
        admin: RevelUser,
        revel_user_factory: t.Any,
        mock_email: MagicMock,
    ) -> None:
        user = revel_user_factory(email="new@example.com")
        ReferralCode.objects.create(user=user, code="oldcode", is_active=False)

        svc.approve(pending, actor=admin)

        code = ReferralCode.objects.get(user=user)
        assert code.is_active and code.code == "oldcode"
        assert code.revenue_share_percent == pending.revenue_share_percent

    def test_existing_active_code_raises_and_leaves_pending(
        self,
        pending: ReferralApplication,
        admin: RevelUser,
        revel_user_factory: t.Any,
        mock_email: MagicMock,
    ) -> None:
        user = revel_user_factory(email="new@example.com")
        ReferralCode.objects.create(user=user, code="already")

        with pytest.raises(ReferralAlreadyActiveError):
            svc.approve(pending, actor=admin)

        pending.refresh_from_db()
        assert pending.status == PENDING
        mock_email.assert_not_called()

    def test_only_pending_can_be_approved(
        self, pending: ReferralApplication, admin: RevelUser, mock_email: MagicMock
    ) -> None:
        svc.reject(pending, actor=admin, admin_note="")
        with pytest.raises(ReferralApplicationConflictError):
            svc.approve(pending, actor=admin)


class TestRejectAndBlock:
    def test_reject_sends_email_with_note(
        self,
        pending: ReferralApplication,
        admin: RevelUser,
        mock_email: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            svc.reject(pending, actor=admin, admin_note="Not yet")
        pending.refresh_from_db()
        assert pending.status == REJECTED and pending.admin_note == "Not yet"
        assert mock_email.call_args.args == ("referral_rejected", "new@example.com")
        assert mock_email.call_args.kwargs["context"] == {"admin_note": "Not yet"}

    def test_block_uses_same_email_and_sets_blocked(
        self,
        pending: ReferralApplication,
        admin: RevelUser,
        mock_email: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            svc.block(pending, actor=admin, admin_note="")
        pending.refresh_from_db()
        assert pending.status == BLOCKED
        assert mock_email.call_args.args == ("referral_rejected", "new@example.com")

    def test_reject_only_from_pending(
        self, pending: ReferralApplication, admin: RevelUser, mock_email: MagicMock
    ) -> None:
        svc.block(pending, actor=admin, admin_note="")
        with pytest.raises(ReferralApplicationConflictError):
            svc.reject(pending, actor=admin, admin_note="")


class TestCreateInvite:
    def test_invite_creates_approved_row_and_sends_invite(
        self, admin: RevelUser, mock_email: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            app = svc.create_invite(
                email="Guest@Example.com",
                code="guest",
                revenue_share_percent=Decimal("25.00"),
                note="Hey!",
                actor=admin,
            )
        assert app.source == ReferralApplication.Source.INVITE and app.status == APPROVED
        assert app.admin_note == "Hey!" and app.email == "guest@example.com"
        assert mock_email.call_args.args == ("referral_invite", "guest@example.com")
        assert mock_email.call_args.kwargs["context"]["admin_note"] == "Hey!"
        assert mock_email.call_args.kwargs["context"]["revenue_share_percent"] == "25.00"

    def test_invite_with_taken_code_raises(self, admin: RevelUser, revel_user_factory: t.Any) -> None:
        ReferralCode.objects.create(user=revel_user_factory(), code="dupe")
        with pytest.raises(ReferralApplicationConflictError):
            svc.create_invite(
                email="x@example.com", code="DUPE", revenue_share_percent=Decimal("15.00"), note="", actor=admin
            )

    def test_invite_for_blocked_email_raises(self, admin: RevelUser) -> None:
        ReferralApplication.objects.create(email="x@example.com", code="old", note="hi", status=BLOCKED)
        with pytest.raises(ReferralApplicationConflictError):
            svc.create_invite(
                email="x@example.com", code="fresh", revenue_share_percent=Decimal("15.00"), note="", actor=admin
            )


class TestEnrollOnSignup:
    def test_signup_with_approved_invite_creates_code(
        self,
        pending: ReferralApplication,
        admin: RevelUser,
        revel_user_factory: t.Any,
        mock_email: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        svc.approve(pending, actor=admin)
        mock_email.reset_mock()

        with django_capture_on_commit_callbacks(execute=True):
            user = revel_user_factory(email="New@Example.com")

        code = ReferralCode.objects.get(user=user)
        assert code.code == "newcode"
        pending.refresh_from_db()
        assert pending.user == user and pending.status == APPROVED
        assert mock_email.call_args.args == ("referral_enrolled", user.email)

    def test_signup_without_invite_is_noop(self, revel_user_factory: t.Any, mock_email: MagicMock) -> None:
        user = revel_user_factory(email="nobody@example.com")
        assert not ReferralCode.objects.filter(user=user).exists()

    def test_pending_application_does_not_enroll(self, pending: ReferralApplication, revel_user_factory: t.Any) -> None:
        user = revel_user_factory(email="new@example.com")
        assert not ReferralCode.objects.filter(user=user).exists()

    def test_code_clash_does_not_block_account_creation(
        self,
        pending: ReferralApplication,
        admin: RevelUser,
        revel_user_factory: t.Any,
        mock_email: MagicMock,
    ) -> None:
        """A referral perk must never break signup: the account is created, enrollment is skipped."""
        svc.approve(pending, actor=admin)
        ReferralCode.objects.create(user=revel_user_factory(email="squatter@example.com"), code="NEWCODE")

        user = revel_user_factory(email="new@example.com")

        assert not ReferralCode.objects.filter(user=user).exists()
        pending.refresh_from_db()
        assert pending.status == APPROVED and pending.user is None
