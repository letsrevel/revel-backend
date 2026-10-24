"""The sweep releases hostnames that are never proven or fixed (#1168). Cloudflare is mocked.

A PENDING domain that has not proven ownership 7 days after the request is failed; a FAILED one unchanged for 7
days is retired, which frees its hostname. Ages are set with queryset updates: ``created_at`` / ``updated_at``
are ``auto_now*`` fields, which the ORM would overwrite on ``save()``.
"""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from events.exceptions import CloudflareAPIError
from events.models import Organization, OrganizationDomain, OrganizationDomainStatus
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import custom_domain_service, dns_diagnostics
from events.tasks import sync_custom_domains_task
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
OLD = timedelta(days=7, minutes=1)
YOUNG = timedelta(days=6, hours=23)


@pytest.fixture(autouse=True)
def platform_settings(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ZONE_ID = "zone"
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False


@pytest.fixture(autouse=True)
def external_calls() -> t.Iterator[None]:
    """No network: DNS lookups are empty, and the notification delivery task is a mock."""
    with (
        patch.object(cloudflare, "txt_record_published", return_value=True),
        patch.object(dns_diagnostics, "diagnose_dns", return_value=[]),
        patch.object(dns_diagnostics, "detect_dns_provider", return_value=None),
        patch("notifications.tasks.dispatch_notification.delay"),
    ):
        yield


@pytest.fixture(autouse=True)
def cloudflare_calls() -> t.Iterator[tuple[MagicMock, MagicMock]]:
    """``(get, delete)``: Cloudflare keeps reporting every hostname as still pending, and deletes succeed."""

    def pending(cf_id: str) -> cloudflare.CustomHostname:
        hostname = cf_id.removeprefix("cf-")
        return cloudflare.CustomHostname(
            id=cf_id, hostname=hostname, status="pending", ssl=cloudflare.CustomHostnameSsl(status="pending_validation")
        )

    with (
        patch.object(cloudflare, "get_custom_hostname", side_effect=pending) as get,
        patch.object(cloudflare, "delete_custom_hostname") as delete,
    ):
        yield get, delete


@pytest.fixture
def make_domain() -> t.Callable[..., OrganizationDomain]:
    counter = iter(range(100))

    def _make(
        hostname: str,
        *,
        status: OrganizationDomainStatus = Status.PENDING,
        age: timedelta = timedelta(0),
        enabled: bool = True,
    ) -> OrganizationDomain:
        n = next(counter)
        owner = get_user_model().objects.create_user(f"owner{n}", f"owner{n}@example.com", "pw")
        org = Organization.objects.create(name=f"Org {n}", slug=f"org-{n}", owner=owner, custom_domain_enabled=enabled)
        domain = OrganizationDomain.objects.create(
            organization=org, hostname=hostname, status=status, cloudflare_hostname_id=f"cf-{hostname}"
        )
        then = timezone.now() - age
        OrganizationDomain.objects.filter(pk=domain.pk).update(created_at=then, updated_at=then)
        if status == Status.ACTIVE:
            Organization.objects.filter(pk=org.pk).update(custom_domain=hostname)
        domain.refresh_from_db()
        return domain

    return _make


def sweep(callbacks: t.Any) -> None:
    with callbacks(execute=True):
        sync_custom_domains_task()


def statuses(*domains: OrganizationDomain) -> list[str]:
    for domain in domains:
        domain.refresh_from_db()
    return [domain.status for domain in domains]


# ---- PENDING expiry ----


def test_a_pending_domain_older_than_seven_days_is_failed_and_its_owner_told(
    make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
) -> None:
    domain = make_domain("old.example.com", age=OLD)

    sweep(django_capture_on_commit_callbacks)

    domain.refresh_from_db()
    assert domain.status == Status.FAILED
    assert domain.last_errors == [custom_domain_service.PENDING_EXPIRED_MESSAGE]
    (notification,) = Notification.objects.filter(notification_type=NotificationType.ORG_CUSTOM_DOMAIN_FAILED)
    assert notification.user == domain.organization.owner
    assert notification.context["errors"] == [custom_domain_service.PENDING_EXPIRED_MESSAGE]
    assert notification.context["was_live"] is False


def test_the_expiry_message_is_appended_to_the_existing_errors(
    settings: t.Any, make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
) -> None:
    settings.CLOUDFLARE_API_TOKEN = ""  # with Cloudflare, the sync step first refreshes ``last_errors``
    domain = make_domain("old.example.com", age=OLD)
    OrganizationDomain.objects.filter(pk=domain.pk).update(last_errors=["No CNAME record was found."])

    sweep(django_capture_on_commit_callbacks)

    domain.refresh_from_db()
    assert domain.last_errors == ["No CNAME record was found.", custom_domain_service.PENDING_EXPIRED_MESSAGE]


def test_expiry_does_not_need_cloudflare(
    settings: t.Any, make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
) -> None:
    settings.CLOUDFLARE_API_TOKEN = ""
    domain = make_domain("old.example.com", age=OLD)

    sweep(django_capture_on_commit_callbacks)

    assert statuses(domain) == [Status.FAILED]


def test_a_younger_pending_domain_and_any_active_domain_are_untouched(
    make_domain: t.Callable[..., OrganizationDomain],
    cloudflare_calls: tuple[MagicMock, MagicMock],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    young = make_domain("young.example.com", age=YOUNG)
    active = make_domain("active.example.com", status=Status.ACTIVE, age=timedelta(days=90))

    sweep(django_capture_on_commit_callbacks)

    assert statuses(young, active) == [Status.PENDING, Status.ACTIVE]
    active.organization.refresh_from_db()
    assert active.organization.custom_domain == "active.example.com"
    assert not Notification.objects.exists()
    assert not cloudflare_calls[1].called


# ---- FAILED retirement ----


def test_a_failed_domain_older_than_seven_days_is_removed_and_its_cloudflare_hostname_deleted(
    make_domain: t.Callable[..., OrganizationDomain],
    cloudflare_calls: tuple[MagicMock, MagicMock],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    domain = make_domain("dead.example.com", status=Status.FAILED, age=OLD)

    sweep(django_capture_on_commit_callbacks)

    assert statuses(domain) == [Status.REMOVED]
    cloudflare_calls[1].assert_called_once_with("cf-dead.example.com")
    assert not Notification.objects.exists()  # REMOVED is never notified


def test_retirement_without_cloudflare_removes_the_row_and_calls_nothing(
    settings: t.Any,
    make_domain: t.Callable[..., OrganizationDomain],
    cloudflare_calls: tuple[MagicMock, MagicMock],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    settings.CLOUDFLARE_API_TOKEN = ""
    domain = make_domain("dead.example.com", status=Status.FAILED, age=OLD)

    sweep(django_capture_on_commit_callbacks)

    assert statuses(domain) == [Status.REMOVED]
    assert not cloudflare_calls[1].called


def test_a_younger_failed_domain_is_untouched(
    make_domain: t.Callable[..., OrganizationDomain],
    cloudflare_calls: tuple[MagicMock, MagicMock],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    domain = make_domain("dead.example.com", status=Status.FAILED, age=YOUNG)

    sweep(django_capture_on_commit_callbacks)

    assert statuses(domain) == [Status.FAILED]
    assert not cloudflare_calls[1].called


def test_a_domain_just_expired_is_retired_only_after_the_retention_window(
    make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
) -> None:
    domain = make_domain("old.example.com", age=timedelta(days=30))

    sweep(django_capture_on_commit_callbacks)
    assert statuses(domain) == [Status.FAILED]  # failed now, not retired in the same sweep

    OrganizationDomain.objects.filter(pk=domain.pk).update(updated_at=timezone.now() - OLD)
    sweep(django_capture_on_commit_callbacks)
    assert statuses(domain) == [Status.REMOVED]


def test_retirement_frees_the_hostname_for_another_organization(
    make_domain: t.Callable[..., OrganizationDomain],
    organization: Organization,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    squatter = make_domain("shared.example.com", status=Status.FAILED, age=OLD)
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])

    sweep(django_capture_on_commit_callbacks)
    new_hostname = cloudflare.CustomHostname(
        id="cf-new", hostname="shared.example.com", status="pending", ssl=cloudflare.CustomHostnameSsl(status="pending")
    )
    with patch.object(cloudflare, "create_custom_hostname", return_value=new_hostname):
        claimed = custom_domain_service.request_domain(organization, "shared.example.com")

    assert statuses(squatter) == [Status.REMOVED]
    assert (claimed.organization, claimed.status) == (organization, Status.PENDING)


# ---- kill switch and isolation ----


def test_organizations_with_the_kill_switch_off_are_skipped(
    make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
) -> None:
    pending = make_domain("pending.example.com", age=OLD, enabled=False)
    failed = make_domain("failed.example.com", status=Status.FAILED, age=OLD, enabled=False)

    sweep(django_capture_on_commit_callbacks)

    assert statuses(pending, failed) == [Status.PENDING, Status.FAILED]


def test_one_failing_expiry_does_not_stop_the_others(
    make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
) -> None:
    first = make_domain("first.example.com", age=OLD)
    broken = make_domain("broken.example.com", age=OLD)
    last = make_domain("last.example.com", age=OLD)
    real = custom_domain_service.mark_domain_failed

    def fake(domain: OrganizationDomain) -> OrganizationDomain:
        if domain.pk == broken.pk:
            raise RuntimeError("boom")
        return real(domain)

    with patch.object(custom_domain_service, "mark_domain_failed", side_effect=fake):
        sweep(django_capture_on_commit_callbacks)

    assert statuses(first, broken, last) == [Status.FAILED, Status.PENDING, Status.FAILED]
    broken.refresh_from_db()
    assert broken.last_errors == []  # the half-done write rolled back with the failed transaction


def test_one_failing_retirement_does_not_stop_the_others_and_keeps_its_hostname_reserved(
    make_domain: t.Callable[..., OrganizationDomain],
    cloudflare_calls: tuple[MagicMock, MagicMock],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    first = make_domain("first.example.com", status=Status.FAILED, age=OLD)
    broken = make_domain("broken.example.com", status=Status.FAILED, age=OLD)
    last = make_domain("last.example.com", status=Status.FAILED, age=OLD)

    def fake_delete(cf_id: str) -> None:
        if cf_id == broken.cloudflare_hostname_id:
            raise CloudflareAPIError("boom", status_code=500)

    cloudflare_calls[1].side_effect = fake_delete

    sweep(django_capture_on_commit_callbacks)

    assert statuses(first, broken, last) == [Status.REMOVED, Status.FAILED, Status.REMOVED]
