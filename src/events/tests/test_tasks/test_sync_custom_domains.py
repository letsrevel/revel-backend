"""Tests for the custom-domain sync task and its beat schedule (#1151). Cloudflare is mocked."""

import importlib
import typing as t
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ValidationError as DjangoValidationError
from django_celery_beat.models import PeriodicTask
from pydantic import ValidationError
from structlog.testing import capture_logs

from events.exceptions import CloudflareAPIError, CustomDomainNotEnabledError
from events.models import Organization, OrganizationDomain, OrganizationDomainStatus
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
        status: OrganizationDomainStatus = Status.PENDING,
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


def test_task_syncs_only_live_provisioned_domains_of_enabled_orgs(
    make_domain: t.Callable[..., OrganizationDomain],
) -> None:
    pending = make_domain("pending.example.com")
    active = make_domain("active.example.com", status=Status.ACTIVE)
    make_domain("manual.example.com", cf_id=None)
    make_domain("failed.example.com", status=Status.FAILED)
    make_domain("removed.example.com", status=Status.REMOVED)
    make_domain("disabled.example.com", enabled=False)
    make_domain("disabled-active.example.com", status=Status.ACTIVE, enabled=False)

    with patch.object(
        cloudflare, "get_custom_hostname", side_effect=lambda cf_id: hosted(cf_id.removeprefix("cf-"))
    ) as get:
        result = sync_custom_domains_task()

    assert {c.args[0] for c in get.call_args_list} == {pending.cloudflare_hostname_id, active.cloudflare_hostname_id}
    assert get.call_count == 2
    assert result == {"synced": 2, "failed": 0}


def test_task_fails_an_active_domain_cloudflare_reports_blocked(
    make_domain: t.Callable[..., OrganizationDomain],
) -> None:
    active = make_domain("active.example.com", status=Status.ACTIVE)
    org = active.organization
    org.custom_domain = "active.example.com"
    org.save(update_fields=["custom_domain"])

    with patch.object(
        cloudflare, "get_custom_hostname", return_value=hosted("active.example.com", "blocked", "active")
    ):
        result = sync_custom_domains_task()

    assert result == {"synced": 1, "failed": 0}
    active.refresh_from_db()
    org.refresh_from_db()
    assert active.status == Status.FAILED
    assert org.custom_domain is None


def test_task_keeps_an_active_domain_through_a_transient_state(
    make_domain: t.Callable[..., OrganizationDomain],
) -> None:
    active = make_domain("active.example.com", status=Status.ACTIVE)

    with patch.object(
        cloudflare, "get_custom_hostname", return_value=hosted("active.example.com", "active", "active_redeploying")
    ):
        sync_custom_domains_task()

    active.refresh_from_db()
    assert active.status == Status.ACTIVE


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


def _pydantic_validation_error() -> ValidationError:
    try:
        cloudflare.CustomHostname.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a validation error")


@pytest.mark.parametrize(
    "error",
    [
        DjangoValidationError("bad row"),
        CustomDomainNotEnabledError(),
        _pydantic_validation_error(),
        RuntimeError("unexpected"),
    ],
    ids=["django-validation", "not-enabled", "pydantic-validation", "runtime"],
)
def test_any_error_on_one_domain_is_isolated_and_the_next_domain_still_syncs(
    make_domain: t.Callable[..., OrganizationDomain], error: Exception
) -> None:
    broken = make_domain("broken.example.com")
    last = make_domain("last.example.com")

    def fake_get(cf_id: str) -> cloudflare.CustomHostname:
        if cf_id == broken.cloudflare_hostname_id:
            raise error
        return hosted(cf_id.removeprefix("cf-"), "active", "active")

    with patch.object(cloudflare, "get_custom_hostname", side_effect=fake_get), capture_logs() as logs:
        result = sync_custom_domains_task()

    assert result == {"synced": 1, "failed": 1}
    broken.refresh_from_db()
    last.refresh_from_db()
    assert (broken.status, last.status) == (Status.PENDING, Status.ACTIVE)
    failures = [log for log in logs if log["event"] == "custom_domain_sync_failed"]
    assert [log["domain_id"] for log in failures] == [str(broken.pk)]


def test_domain_failing_during_the_transition_does_not_stop_the_next_domain(
    make_domain: t.Callable[..., OrganizationDomain],
) -> None:
    """The kill switch flipping between the query and the activation raises inside the transition."""
    first = make_domain("first.example.com")
    second = make_domain("second.example.com")
    first.organization.custom_domain_enabled = False
    first.organization.save(update_fields=["custom_domain_enabled"])
    domains = [first, second]
    # The sweep's query excludes disabled orgs, so hand it the stale snapshot a mid-sweep flip would leave.
    with (
        patch.object(
            cloudflare, "get_custom_hostname", side_effect=lambda cf_id: hosted(cf_id[3:], "active", "active")
        ),
        patch("events.tasks.custom_domains.OrganizationDomain") as model,  # only the task's name, not the class
    ):
        model.Status = Status
        model.objects.filter.return_value.select_related.return_value = domains
        result = sync_custom_domains_task()

    assert result == {"synced": 1, "failed": 1}
    second.refresh_from_db()
    assert second.status == Status.ACTIVE


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


def test_reapplying_the_beat_migration_never_reenables_an_admin_disabled_task() -> None:
    """``get_or_create`` (precedent: migration 0125): an admin's switch-off survives a re-apply."""
    migration = importlib.import_module("events.migrations.0135_sync_custom_domains_beat")
    row = PeriodicTask.objects.get(task="events.sync_custom_domains")
    row.enabled = False
    row.save(update_fields=["enabled"])

    migration.create_sync_task(django_apps, None)

    row.refresh_from_db()
    assert row.enabled is False
    assert PeriodicTask.objects.filter(task="events.sync_custom_domains").count() == 1
    assert row.name == "Sync pending and active custom domains from Cloudflare"
