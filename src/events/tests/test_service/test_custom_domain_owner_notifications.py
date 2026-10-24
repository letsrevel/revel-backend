"""The organization owner is emailed when a custom domain goes live or fails (white-label, #1148).

The hook lives in ``custom_domain_service._save_status``, so every writer of the status is covered:
the scheduled sync and **Check now** (``sync_domain_status``) and the admin actions
(``activate_domain`` / ``mark_domain_failed``). ``on_commit`` callbacks are executed explicitly: the
test transaction never commits, so they would not fire otherwise.
"""

import typing as t
from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Organization, OrganizationDomain, OrganizationStaff
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import custom_domain_service, dns_diagnostics
from notifications.context_schemas import NOTIFICATION_CONTEXT_SCHEMAS
from notifications.enums import MANDATORY_TYPES, TRANSACTIONAL_TYPES, NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
HOSTNAME = "events.example.com"
ACTIVE = NotificationType.ORG_CUSTOM_DOMAIN_ACTIVE
FAILED = NotificationType.ORG_CUSTOM_DOMAIN_FAILED
OWNER_TYPES = [ACTIVE, FAILED]


def hosted(status: str, ssl_status: str | None, errors: list[str] | None = None) -> cloudflare.CustomHostname:
    return cloudflare.CustomHostname(
        id="cf-1",
        hostname=HOSTNAME,
        status=status,
        ssl=cloudflare.CustomHostnameSsl(status=ssl_status) if ssl_status else None,
        ownership_verification=cloudflare.OwnershipVerification(
            type="txt", name=f"_cf-custom-hostname.{HOSTNAME}", value="owner-token"
        ),
        verification_errors=errors or [],
    )


@pytest.fixture(autouse=True)
def platform_settings(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ZONE_ID = "zone"
    settings.CUSTOM_DOMAIN_CNAME_TARGET = "customers.letsrevel.io"
    site = SiteSettings.get_solo()
    site.frontend_base_url = "https://app.letsrevel.io"
    site.save()


@pytest.fixture(autouse=True)
def external_checks() -> t.Iterator[None]:
    """No network: the ownership TXT is published, the DNS pre-flight has nothing to add."""
    with (
        patch.object(cloudflare, "txt_record_published", return_value=True),
        patch.object(dns_diagnostics, "diagnose_dns", return_value=[]),
    ):
        yield


@pytest.fixture(autouse=True)
def dispatch() -> t.Iterator[MagicMock]:
    """The Celery delivery task, so the notification rows can be inspected without sending."""
    with patch("notifications.tasks.dispatch_notification.delay") as delay:
        yield delay


@pytest.fixture
def enabled_org(organization: Organization) -> Organization:
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    return organization


@pytest.fixture
def pending_domain(enabled_org: Organization) -> OrganizationDomain:
    return OrganizationDomain.objects.create(
        organization=enabled_org, hostname=HOSTNAME, cloudflare_hostname_id="cf-1", status=Status.PENDING
    )


@pytest.fixture
def active_domain(pending_domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any) -> OrganizationDomain:
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.activate_domain(pending_domain)
    Notification.objects.all().delete()
    return pending_domain


@pytest.fixture
def failed_domain(pending_domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any) -> OrganizationDomain:
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.mark_domain_failed(pending_domain)
    Notification.objects.all().delete()
    return pending_domain


def sent(*types: NotificationType) -> list[Notification]:
    return list(Notification.objects.filter(notification_type__in=types or OWNER_TYPES).order_by("created_at"))


def run_sync(domain: OrganizationDomain, result: cloudflare.CustomHostname, callbacks: t.Any) -> None:
    with patch.object(cloudflare, "get_custom_hostname", return_value=result), callbacks(execute=True):
        custom_domain_service.sync_domain_status(domain)


# ---- type registration ----


def test_types_are_opt_out_able_transactional_and_have_context_schemas() -> None:
    """Preference-gated, not mandatory; the owner is waiting on them, so they skip the digest."""
    for notification_type in OWNER_TYPES:
        assert notification_type not in MANDATORY_TYPES
        assert notification_type in TRANSACTIONAL_TYPES
        assert notification_type in NOTIFICATION_CONTEXT_SCHEMAS


# ---- PENDING -> ACTIVE ----


def test_activation_notifies_the_owner_with_a_tenant_link_and_a_platform_admin_link(
    pending_domain: OrganizationDomain,
    organization_owner_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
    dispatch: MagicMock,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.activate_domain(pending_domain)

    (notification,) = sent()
    assert notification.notification_type == ACTIVE
    assert notification.user == organization_owner_user
    assert notification.context == {
        "organization_id": str(pending_domain.organization_id),
        "organization_name": "Org",
        "organization_slug": "org",
        "hostname": HOSTNAME,
        "domain_url": f"https://{HOSTNAME}",
        "action_url": "https://app.letsrevel.io/org/org/admin/custom-domain",
        "frontend_url": f"https://{HOSTNAME}",
    }
    dispatch.assert_called_once_with(str(notification.id))


def test_nothing_is_requested_before_the_transaction_commits(
    pending_domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any, dispatch: MagicMock
) -> None:
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        custom_domain_service.activate_domain(pending_domain)

    assert sent() == []
    dispatch.assert_not_called()
    for callback in callbacks:
        callback()
    assert len(sent()) == 1


def test_sync_activation_notifies_once_and_a_second_sweep_does_not(
    pending_domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any
) -> None:
    run_sync(pending_domain, hosted("active", "active"), django_capture_on_commit_callbacks)
    run_sync(pending_domain, hosted("active", "active"), django_capture_on_commit_callbacks)

    assert [n.notification_type for n in sent()] == [ACTIVE]


# ---- ... -> FAILED ----


def test_pending_to_failed_notifies_with_the_reasons_and_a_platform_link(
    pending_domain: OrganizationDomain,
    organization_owner_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    OrganizationDomain.objects.filter(pk=pending_domain.pk).update(last_errors=["No CNAME record was found."])
    pending_domain.refresh_from_db()

    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.mark_domain_failed(pending_domain)

    (notification,) = sent()
    assert notification.notification_type == FAILED
    assert notification.user == organization_owner_user
    assert notification.context["hostname"] == HOSTNAME
    assert notification.context["was_live"] is False
    assert notification.context["errors"] == ["No CNAME record was found."]
    assert notification.context["action_url"] == "https://app.letsrevel.io/org/org/admin/custom-domain"
    assert notification.context["frontend_url"] == notification.context["action_url"]
    assert "domain_url" not in notification.context


def test_active_to_failed_says_the_domain_was_live(
    active_domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.mark_domain_failed(active_domain)

    (notification,) = sent()
    assert notification.notification_type == FAILED
    assert notification.context["was_live"] is True
    assert HOSTNAME not in notification.context["action_url"]  # admin links never use the tenant domain


@pytest.mark.parametrize(
    ("start", "result", "reason"),
    [
        (Status.PENDING, hosted("blocked", "pending_validation", ["Hostname is blocked."]), "blocked"),
        (Status.PENDING, hosted("moved", "active"), "moved"),
        (Status.ACTIVE, hosted("moved", "active"), "moved"),
    ],
)
def test_sync_failure_notifies_with_cloudflares_messages(
    request: pytest.FixtureRequest,
    start: str,
    result: cloudflare.CustomHostname,
    reason: str,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    domain = request.getfixturevalue("active_domain" if start == Status.ACTIVE else "pending_domain")

    run_sync(domain, result, django_capture_on_commit_callbacks)

    (notification,) = sent()
    assert notification.notification_type == FAILED
    assert notification.context["was_live"] is (start == Status.ACTIVE)
    assert any(reason in message for message in notification.context["errors"])


# ---- no mail ----


def test_a_sync_that_changes_nothing_sends_nothing(
    pending_domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any
) -> None:
    run_sync(pending_domain, hosted("pending", "pending_validation"), django_capture_on_commit_callbacks)

    assert pending_domain.status == Status.PENDING
    assert sent() == []


def test_an_active_domain_that_stays_active_sends_nothing(
    active_domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any
) -> None:
    run_sync(active_domain, hosted("active", "active"), django_capture_on_commit_callbacks)
    run_sync(active_domain, hosted("active", "validation_timed_out"), django_capture_on_commit_callbacks)

    assert active_domain.status == Status.ACTIVE
    assert sent() == []


def test_activating_an_active_domain_again_sends_nothing(
    active_domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.activate_domain(active_domain)

    assert sent() == []


def test_failing_a_failed_domain_again_sends_nothing(
    failed_domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.mark_domain_failed(failed_domain)

    assert sent() == []


def test_staff_reactivating_a_failed_domain_sends_nothing(
    failed_domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Only staff can do this from the admin; the owner is not told about a recovery they did not trigger."""
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.activate_domain(failed_domain)

    assert failed_domain.status == Status.ACTIVE
    assert sent() == []


@pytest.mark.parametrize("fixture", ["pending_domain", "active_domain", "failed_domain"])
def test_removal_sends_nothing(
    request: pytest.FixtureRequest, fixture: str, django_capture_on_commit_callbacks: t.Any
) -> None:
    domain = request.getfixturevalue(fixture)

    with patch.object(cloudflare, "delete_custom_hostname"), django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.delete_domain(domain)

    assert domain.status == Status.REMOVED
    assert sent() == []


# ---- recipients and preferences ----


def test_only_the_owner_is_notified_not_staff(
    pending_domain: OrganizationDomain,
    organization_owner_user: RevelUser,
    staff_member: OrganizationStaff,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.activate_domain(pending_domain)

    assert [n.user_id for n in sent()] == [organization_owner_user.pk]


def test_an_owner_who_opted_out_of_the_type_is_not_notified_but_still_gets_the_other_type(
    pending_domain: OrganizationDomain,
    organization_owner_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    preferences = organization_owner_user.notification_preferences
    preferences.notification_type_settings = {ACTIVE.value: {"enabled": False, "channels": []}}
    preferences.save()

    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.activate_domain(pending_domain)
    assert sent() == []

    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.mark_domain_failed(pending_domain)
    assert [n.notification_type for n in sent()] == [FAILED]


def test_an_owner_who_silenced_everything_is_not_notified(
    pending_domain: OrganizationDomain,
    organization_owner_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    preferences = organization_owner_user.notification_preferences
    preferences.silence_all_notifications = True
    preferences.save()

    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.mark_domain_failed(pending_domain)

    assert sent() == []
