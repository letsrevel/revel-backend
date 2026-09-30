"""Tests for the email policy service: may_email, suppression_for, suppress."""

import pytest

from accounts.models import RevelUser
from events.models import Organization
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import EmailSuppression, NotificationPreference
from notifications.service.email_policy import may_email, suppress, suppression_for

pytestmark = pytest.mark.django_db

Reason = EmailSuppression.Reason
Source = EmailSuppression.Source


class TestMayEmail:
    def test_true_when_email_channel_effective(self, user: RevelUser) -> None:
        prefs = NotificationPreference.objects.get(user=user)
        prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.EMAIL]
        prefs.save()
        user.refresh_from_db()
        assert may_email(user, NotificationType.EVENT_REMINDER) is True

    def test_false_when_email_off(self, user: RevelUser) -> None:
        prefs = NotificationPreference.objects.get(user=user)
        prefs.enabled_channels = [DeliveryChannel.IN_APP]
        prefs.save()
        user.refresh_from_db()
        assert may_email(user, NotificationType.EVENT_REMINDER) is False
        assert may_email(user, NotificationType.TICKET_CREATED) is True

    def test_false_without_address(self, user: RevelUser) -> None:
        user.email = ""
        assert may_email(user, NotificationType.TICKET_CREATED) is False


class TestSuppress:
    def test_creates_normalized_row(self) -> None:
        row = suppress("Foo.Bar+tag@Gmail.com", Reason.HARD_BOUNCE, Source.PROVIDER, detail="550")
        assert row.email == "foobar@gmail.com"
        assert (row.reason, row.source, row.detail) == (Reason.HARD_BOUNCE, Source.PROVIDER, "550")

    def test_replay_is_noop(self) -> None:
        first = suppress("a@example.com", Reason.HARD_BOUNCE, Source.PROVIDER, detail="first")
        again = suppress("a@example.com", Reason.HARD_BOUNCE, Source.PROVIDER, detail="second")
        assert again.pk == first.pk
        again.refresh_from_db()
        assert again.detail == "first"
        assert EmailSuppression.objects.count() == 1

    def test_equal_rank_does_not_overwrite(self) -> None:
        suppress("a@example.com", Reason.HARD_BOUNCE, Source.PROVIDER)
        row = suppress("a@example.com", Reason.BLOCKED, Source.PROVIDER)
        row.refresh_from_db()
        assert row.reason == Reason.HARD_BOUNCE

    def test_lower_rank_does_not_downgrade(self, organization: Organization) -> None:
        suppress("a@example.com", Reason.COMPLAINT, Source.PROVIDER, organization_id=organization.id)
        row = suppress("a@example.com", Reason.INVITATION_OPT_OUT, Source.RECIPIENT)
        row.refresh_from_db()
        assert (row.reason, row.source, row.organization_id) == (Reason.COMPLAINT, Source.PROVIDER, organization.id)

    def test_higher_rank_upgrades(self, organization: Organization) -> None:
        suppress("a@example.com", Reason.INVITATION_OPT_OUT, Source.RECIPIENT)
        suppress("a@example.com", Reason.HARD_BOUNCE, Source.PROVIDER, detail="bounce")
        row = suppress(
            "a@example.com", Reason.COMPLAINT, Source.PROVIDER, organization_id=organization.id, detail="spam"
        )
        row.refresh_from_db()
        assert (row.reason, row.source, row.organization_id, row.detail) == (
            Reason.COMPLAINT,
            Source.PROVIDER,
            organization.id,
            "spam",
        )
        assert EmailSuppression.objects.count() == 1


class TestSuppressionFor:
    def test_none_when_absent(self) -> None:
        assert suppression_for("nobody@example.com") is None

    def test_matches_normalized(self) -> None:
        row = suppress("a@example.com", Reason.HARD_BOUNCE, Source.PROVIDER)
        assert suppression_for("A+x@Example.com") == row

    def test_opt_out_excluded_by_default(self) -> None:
        row = suppress("a@example.com", Reason.INVITATION_OPT_OUT, Source.RECIPIENT)
        assert suppression_for("a@example.com") is None
        assert suppression_for("a@example.com", include_opt_out=True) == row

    def test_hard_reasons_count_for_both(self) -> None:
        row = suppress("a@example.com", Reason.COMPLAINT, Source.PROVIDER)
        assert suppression_for("a@example.com") == row
        assert suppression_for("a@example.com", include_opt_out=True) == row
