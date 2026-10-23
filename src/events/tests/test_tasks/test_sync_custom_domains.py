"""Tests for the custom-domain sync task and its beat schedule (#1151). Cloudflare is mocked."""

import typing as t
from unittest.mock import patch

import pytest
from django_celery_beat.models import PeriodicTask

from events.exceptions import CloudflareAPIError
from events.models import Organization, OrganizationDomain
from events.service import cloudflare_custom_hostnames as cloudflare
from events.tasks import sync_custom_domains_task

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status


def hosted(hostname: str, status: str = "pending", ssl_status: str = "pending_validation") -> cloudflare.CustomHostname:
    return cloudflare.CustomHostname(
        id=f"cf-{hostname}", hostname=hostname, status=status, ssl=cloudflare.CustomHostnameSsl(status=ssl_status)
    )


@pytest.fixture(autouse=True)
def configured(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ZONE_ID = "zone"
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False


@pytest.fixture
def make_domain(django_user_model: t.Any) -> t.Callable[..., OrganizationDomain]:
    counter = iter(range(100))

    def _make(
        hostname: str,
        *,
        status: OrganizationDomain.Status = Status.PENDING,
        cf_id: str | None = "set",
        enabled: bool = True,
    ) -> OrganizationDomain:
        n = next(counter)
        owner = django_user_model.objects.create_user(f"owner{n}", f"owner{n}@example.com", "pw")
        org = Organization.objects.create(name=f"Org {n}", slug=f"org-{n}", owner=owner, custom_domain_enabled=enabled)
        return OrganizationDomain.objects.create(
            organization=org,
            hostname=hostname,
            status=status,
            cloudflare_hostname_id=f"cf-{hostname}" if cf_id == "set" else cf_id,
        )

    return _make


def test_task_syncs_only_pending_provisioned_domains_of_enabled_orgs(
    make_domain: t.Callable[..., OrganizationDomain],
) -> None:
    pending = make_domain("pending.example.com")
    make_domain("manual.example.com", cf_id=None)
    make_domain("active.example.com", status=Status.ACTIVE)
    make_domain("failed.example.com", status=Status.FAILED)
    make_domain("disabled.example.com", enabled=False)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted("pending.example.com")) as get:
        result = sync_custom_domains_task()

    get.assert_called_once_with(pending.cloudflare_hostname_id)
    assert result == {"synced": 1, "failed": 0}


def test_task_applies_transitions(make_domain: t.Callable[..., OrganizationDomain]) -> None:
    ready = make_domain("ready.example.com")
    dead = make_domain("dead.example.com")
    waiting = make_domain("waiting.example.com")
    responses = {
        "cf-ready.example.com": hosted("ready.example.com", "active", "active"),
        "cf-dead.example.com": hosted("dead.example.com", "blocked"),
        "cf-waiting.example.com": hosted("waiting.example.com"),
    }

    with patch.object(cloudflare, "get_custom_hostname", side_effect=lambda cf_id: responses[cf_id]):
        result = sync_custom_domains_task()

    assert result == {"synced": 3, "failed": 0}
    for domain in (ready, dead, waiting):
        domain.refresh_from_db()
    assert (ready.status, dead.status, waiting.status) == (Status.ACTIVE, Status.FAILED, Status.PENDING)


def test_one_failing_domain_does_not_stop_the_rest(make_domain: t.Callable[..., OrganizationDomain]) -> None:
    first = make_domain("first.example.com")
    broken = make_domain("broken.example.com")
    last = make_domain("last.example.com")

    def fake_get(cf_id: str) -> cloudflare.CustomHostname:
        if cf_id == broken.cloudflare_hostname_id:
            raise CloudflareAPIError("boom", status_code=500)
        return hosted(cf_id.removeprefix("cf-"), "active", "active")

    with patch.object(cloudflare, "get_custom_hostname", side_effect=fake_get):
        result = sync_custom_domains_task()

    assert result == {"synced": 2, "failed": 1}
    for domain in (first, broken, last):
        domain.refresh_from_db()
    assert (first.status, broken.status, last.status) == (Status.ACTIVE, Status.PENDING, Status.ACTIVE)


def test_task_is_a_noop_without_cloudflare_config(
    settings: t.Any, make_domain: t.Callable[..., OrganizationDomain]
) -> None:
    settings.CLOUDFLARE_API_TOKEN = ""
    make_domain("pending.example.com")

    with patch.object(cloudflare, "get_custom_hostname") as get:
        result = sync_custom_domains_task()

    get.assert_not_called()
    assert result == {"synced": 0, "failed": 0}


def test_task_is_registered_under_its_pinned_name() -> None:
    assert sync_custom_domains_task.name == "events.sync_custom_domains"


def test_beat_row_runs_every_15_minutes() -> None:
    """The data migration seeds the schedule (the test DB is built by running migrations)."""
    row = PeriodicTask.objects.get(task="events.sync_custom_domains")

    assert row.enabled is True
    assert row.interval is not None
    assert (row.interval.every, row.interval.period) == (15, "minutes")
