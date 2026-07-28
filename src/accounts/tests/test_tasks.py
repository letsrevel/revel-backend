"""Tests for the accounts tasks."""

import functools
import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone

from accounts.models import RevelUser, UserDataExport
from accounts.tasks import (
    DATA_EXPORT_URL_EXPIRES_IN,
    cleanup_expired_data_exports,
    delete_user_account,
    generate_user_data_export,
)


@pytest.mark.django_db
def test_cleanup_expired_data_exports_deletes_old_files(user: RevelUser) -> None:
    """Test that files from expired exports are deleted."""
    export = UserDataExport.objects.create(
        user=user,
        status=UserDataExport.UserDataExportStatus.READY,
        completed_at=timezone.now() - timedelta(seconds=DATA_EXPORT_URL_EXPIRES_IN + 1),
    )
    export.file.save("test_export.zip", ContentFile(b"test content"), save=True)
    assert export.file.name

    result = cleanup_expired_data_exports()

    export.refresh_from_db()
    assert not export.file.name
    assert result == {"files_deleted": 1}


@pytest.mark.django_db
def test_cleanup_expired_data_exports_ignores_recent_exports(user: RevelUser) -> None:
    """Test that recent exports are not touched."""
    export = UserDataExport.objects.create(
        user=user,
        status=UserDataExport.UserDataExportStatus.READY,
        completed_at=timezone.now() - timedelta(days=1),
    )
    export.file.save("test_export.zip", ContentFile(b"test content"), save=True)
    assert export.file.name

    result = cleanup_expired_data_exports()

    export.refresh_from_db()
    assert export.file.name
    assert result == {"files_deleted": 0}


@pytest.mark.django_db(transaction=True)
def test_generate_user_data_export_sends_failure_email(
    user: RevelUser, staff_user: RevelUser, mailoutbox: list[MagicMock]
) -> None:
    """Test that the failure email is sent when the data export fails, then exception is re-raised."""
    with (
        patch("accounts.service.gdpr.generate_user_data_export", side_effect=Exception("Export failed")),
        patch(
            "common.tasks.to_safe_email_address",
        ) as to_safe_email_address_mock,
        pytest.raises(Exception, match="Export failed"),
    ):
        to_safe_email_address_mock.side_effect = lambda e, site_settings=None: e
        generate_user_data_export(str(user.id))

    # Emails should have been sent before the exception was re-raised
    assert len(mailoutbox) == 2

    user_email_sent = False
    admin_email_sent = False

    for email in mailoutbox:
        # Single recipients go to 'to', multiple recipients use 'bcc'
        recipients = email.to + email.bcc
        if user.email in recipients:
            assert email.subject == "Your Revel Data Export has Failed"
            user_email_sent = True
        if staff_user.email in recipients:
            assert email.subject == "User Data Export Failed"
            admin_email_sent = True

    assert user_email_sent
    assert admin_email_sent


# --- Account deletion vs. live subscriptions (GDPR erasure must stop billing) -------


@pytest.fixture
def subscriber(revel_user_factory: t.Any) -> RevelUser:
    """A user with a live subscription, about to exercise their right to erasure."""
    return t.cast(RevelUser, revel_user_factory(username="subscriber@example.com", email="subscriber@example.com"))


@pytest.fixture
def host_organization(revel_user_factory: t.Any) -> t.Any:
    """An organization owned by somebody *other* than the user being deleted."""
    from events.models import Organization

    owner = revel_user_factory(username="orgowner@example.com", email="orgowner@example.com")
    return Organization.objects.create(
        name="Deletion Org", slug="deletion-org", owner=owner, stripe_account_id="acct_test"
    )


def _subscribe(organization: t.Any, user: RevelUser, *, online: bool, stripe_id: str = "") -> t.Any:
    """Give ``user`` an ACTIVE subscription on a fresh plan of ``organization``."""
    from events.models import MembershipSubscription, MembershipSubscriptionPlan, MembershipTier

    tier = MembershipTier.objects.get(organization=organization, name="General membership")
    payment_method = (
        MembershipSubscriptionPlan.PaymentMethod.ONLINE if online else MembershipSubscriptionPlan.PaymentMethod.OFFLINE
    )
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name=f"Monthly {payment_method}",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        payment_method=payment_method,
        stripe_product_id="prod_test" if online else "",
        stripe_price_id="price_test" if online else "",
    )
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        stripe_subscription_id=stripe_id,
    )


def _run_stripe_cancels(callbacks: list[t.Any]) -> None:
    """Run the post-commit Stripe cancels captured during deletion, and only those.

    The cascade also schedules notification callbacks (membership-removed,
    cancellation-confirmed) that re-read rows the deletion has just removed —
    pre-existing behaviour of deleting any organization member, unrelated to the
    Stripe leg under test. ``cancel_subscriptions_for_membership_loss`` is the
    only scheduler here that uses a ``functools.partial``, which makes the
    Stripe callbacks unambiguous to pick out.
    """
    for callback in callbacks:
        if isinstance(callback, functools.partial):
            callback()


@pytest.mark.django_db
def test_delete_user_account_cancels_live_online_subscription(
    subscriber: RevelUser,
    host_organization: t.Any,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Erasure closes the Stripe subscription even though the row holding its id is gone.

    This is the ordering crux: the cancel runs *after* the cascade, so it must
    work off the loaded instance rather than re-reading the deleted row.
    """
    from events.models import MembershipSubscription

    subscription = _subscribe(host_organization, subscriber, online=True, stripe_id="sub_live_gdpr")
    user_id = subscriber.id

    with patch("stripe.Subscription.cancel") as stripe_cancel:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            delete_user_account(str(user_id))

        assert not RevelUser.objects.filter(id=user_id).exists()
        assert not MembershipSubscription.objects.filter(pk=subscription.pk).exists()
        _run_stripe_cancels(callbacks)

    stripe_cancel.assert_called_once_with("sub_live_gdpr", stripe_account="acct_test")


@pytest.mark.django_db
def test_delete_user_account_proceeds_when_stripe_is_down(
    subscriber: RevelUser,
    host_organization: t.Any,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """A Stripe outage must not block the erasure — the cancel is best-effort."""
    import stripe

    _subscribe(host_organization, subscriber, online=True, stripe_id="sub_live_gdpr")
    user_id = subscriber.id

    with patch(
        "stripe.Subscription.cancel", side_effect=stripe.error.APIConnectionError("stripe is down")
    ) as stripe_cancel:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            delete_user_account(str(user_id))
        _run_stripe_cancels(callbacks)

    stripe_cancel.assert_called_once_with("sub_live_gdpr", stripe_account="acct_test")
    assert not RevelUser.objects.filter(id=user_id).exists()


@pytest.mark.django_db
def test_delete_user_account_proceeds_when_cancellation_raises(
    subscriber: RevelUser,
    host_organization: t.Any,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Any failure in the cancellation path is logged, never allowed to abort the erasure."""
    _subscribe(host_organization, subscriber, online=True, stripe_id="sub_live_gdpr")
    user_id = subscriber.id

    with patch(
        "events.service.subscription_service.cancel_subscriptions_for_membership_loss",
        side_effect=Exception("boom"),
    ) as cancel:
        with django_capture_on_commit_callbacks(execute=False):
            delete_user_account(str(user_id))

    cancel.assert_called_once()
    assert not RevelUser.objects.filter(id=user_id).exists()


@pytest.mark.django_db
def test_delete_user_account_offline_subscription_is_local_only(
    subscriber: RevelUser,
    host_organization: t.Any,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """OFFLINE plans have no Stripe side to close — terminalize locally and delete."""
    from events.models import MembershipSubscription
    from events.service import subscription_service

    subscription = _subscribe(host_organization, subscriber, online=False)
    user_id = subscriber.id

    with (
        patch("stripe.Subscription.cancel") as stripe_cancel,
        patch.object(
            subscription_service,
            "cancel_subscriptions_for_membership_loss",
            wraps=subscription_service.cancel_subscriptions_for_membership_loss,
        ) as cancel,
    ):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            delete_user_account(str(user_id))
        _run_stripe_cancels(callbacks)

    cancel.assert_called_once()
    # The user instance is the one the deletion nulled the pk on, so compare the org only.
    assert cancel.call_args.args[1].pk == subscription.organization_id
    stripe_cancel.assert_not_called()
    assert not RevelUser.objects.filter(id=user_id).exists()
    assert not MembershipSubscription.objects.filter(user_id=user_id).exists()
