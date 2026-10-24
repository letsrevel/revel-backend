"""Platform alerts when an ACTIVE custom domain develops a problem or fails (#1148 review, S3). Cloudflare is mocked."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.core import mail
from structlog.testing import capture_logs

from accounts.models import RevelUser
from events.models import Organization, OrganizationDomain
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import custom_domain_service, dns_diagnostics
from events.tasks import notify_custom_domain_problem

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
HOSTNAME = "events.example.com"


class Sweep(t.NamedTuple):
    """What one ``sync_domain_status`` run emitted."""

    logs: list[t.MutableMapping[str, t.Any]]
    dispatched: list[tuple[t.Any, ...]]


def hosted(status: str = "active", ssl_status: str | None = "active") -> cloudflare.CustomHostname:
    return cloudflare.CustomHostname(
        id="cf-1",
        hostname=HOSTNAME,
        status=status,
        ssl=cloudflare.CustomHostnameSsl(status=ssl_status) if ssl_status else None,
    )


@pytest.fixture(autouse=True)
def cloudflare_configured(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ZONE_ID = "zone"
    settings.CUSTOM_DOMAIN_CNAME_TARGET = "customers.letsrevel.io"


@pytest.fixture(autouse=True)
def no_dns_preflight() -> t.Iterator[MagicMock]:
    """The DNS pre-flight hints are tested in ``test_dns_diagnostics`` (no network); tests may set ``return_value``."""
    with patch.object(dns_diagnostics, "diagnose_dns", return_value=[]) as mock:
        yield mock


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
def active_domain(pending_domain: OrganizationDomain) -> OrganizationDomain:
    custom_domain_service.activate_domain(pending_domain)
    return pending_domain


@pytest.fixture
def sweep(
    django_capture_on_commit_callbacks: t.Any,
) -> t.Callable[[OrganizationDomain, cloudflare.CustomHostname], Sweep]:
    """Run one sweep over a domain with Cloudflare answering ``result``; on_commit callbacks fire."""

    def run(domain: OrganizationDomain, result: cloudflare.CustomHostname) -> Sweep:
        with (
            patch.object(cloudflare, "get_custom_hostname", return_value=result),
            patch("events.tasks.custom_domains.notify_custom_domain_problem.delay") as delay,
            capture_logs() as logs,
            django_capture_on_commit_callbacks(execute=True),
        ):
            custom_domain_service.sync_domain_status(domain)
        alerts = [log for log in logs if log["event"].startswith("custom_domain_active_")]
        return Sweep(alerts, [call.args for call in delay.call_args_list])

    return run


def test_active_domain_new_problem_logs_and_notifies_once(
    active_domain: OrganizationDomain, sweep: t.Callable[..., Sweep]
) -> None:
    first = sweep(active_domain, hosted("active", "validation_timed_out"))

    assert [log["event"] for log in first.logs] == ["custom_domain_active_problem"]
    log = first.logs[0]
    assert log["log_level"] == "warning"
    assert log["domain_id"] == str(active_domain.pk)
    assert log["hostname"] == HOSTNAME
    assert log["organization_id"] == str(active_domain.organization_id)
    assert any("'validation_timed_out'" in problem for problem in log["problems"])
    assert first.dispatched == [(str(active_domain.pk), "custom_domain_active_problem")]

    # The next 15-minute sweep sees the same problem: no second alert.
    active_domain.refresh_from_db()
    second = sweep(active_domain, hosted("active", "validation_timed_out"))
    assert second == Sweep([], [])


def test_active_domain_alerts_again_after_recovering_and_relapsing(
    active_domain: OrganizationDomain, sweep: t.Callable[..., Sweep]
) -> None:
    sweep(active_domain, hosted("active", "validation_timed_out"))
    active_domain.refresh_from_db()
    assert sweep(active_domain, hosted("active", "active")) == Sweep([], [])
    active_domain.refresh_from_db()
    assert active_domain.last_errors == []

    relapse = sweep(active_domain, hosted("active", "issuance_timed_out"))

    assert [log["event"] for log in relapse.logs] == ["custom_domain_active_problem"]


def test_healthy_active_domain_does_not_alert(active_domain: OrganizationDomain, sweep: t.Callable[..., Sweep]) -> None:
    assert sweep(active_domain, hosted("active", "active")) == Sweep([], [])


def test_active_domain_failing_alerts_as_failed_only(
    active_domain: OrganizationDomain, sweep: t.Callable[..., Sweep]
) -> None:
    result = sweep(active_domain, hosted("blocked", "active"))

    active_domain.refresh_from_db()
    assert active_domain.status == Status.FAILED
    assert [log["event"] for log in result.logs] == ["custom_domain_active_failed"]
    assert result.dispatched == [(str(active_domain.pk), "custom_domain_active_failed")]


def test_active_domain_moved_fails_and_alerts_as_failed(
    active_domain: OrganizationDomain, sweep: t.Callable[..., Sweep]
) -> None:
    """DNS no longer points at us: the domain fails at once (signing out its tenant sessions) and staff are told."""
    result = sweep(active_domain, hosted("moved", "active"))

    active_domain.refresh_from_db()
    assert active_domain.status == Status.FAILED
    assert [log["event"] for log in result.logs] == ["custom_domain_active_failed"]
    assert any("'moved'" in problem for problem in result.logs[0]["problems"])
    assert result.dispatched == [(str(active_domain.pk), "custom_domain_active_failed")]


def test_pending_domain_failing_does_not_alert(
    pending_domain: OrganizationDomain, sweep: t.Callable[..., Sweep]
) -> None:
    assert sweep(pending_domain, hosted("moved", "pending_validation")) == Sweep([], [])


class TestNotifyTask:
    def test_emails_active_staff_and_superusers_only(
        self, active_domain: OrganizationDomain, django_user_model: t.Any
    ) -> None:
        django_user_model.objects.create_user(username="s@x.io", email="s@x.io", password="p", is_staff=True)
        django_user_model.objects.create_user(username="su@x.io", email="su@x.io", password="p", is_superuser=True)
        django_user_model.objects.create_user(
            username="gone@x.io", email="gone@x.io", password="p", is_staff=True, is_active=False
        )
        django_user_model.objects.create_user(username="u@x.io", email="u@x.io", password="p")
        OrganizationDomain.objects.filter(pk=active_domain.pk).update(last_errors=["Cloudflare says moved"])

        notify_custom_domain_problem(str(active_domain.pk), "custom_domain_active_problem")

        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        # Non-live emails are rewritten to ``internal+<local>_at_<domain>@...`` addresses.
        recipients = " ".join(message.to + message.bcc)
        assert "s_at_x_dot_io" in recipients
        assert "su_at_x_dot_io" in recipients
        assert "gone_at" not in recipients
        assert "u_at_x_dot_io" not in recipients.replace("su_at_x_dot_io", "")
        assert HOSTNAME in message.subject
        assert "Cloudflare says moved" in message.body

    def test_failed_event_has_failed_headline(self, active_domain: OrganizationDomain, user: RevelUser) -> None:
        RevelUser.objects.filter(pk=user.pk).update(is_staff=True)

        notify_custom_domain_problem(str(active_domain.pk), "custom_domain_active_failed")

        assert mail.outbox[0].subject.startswith("Custom domain failed")

    def test_missing_domain_or_no_staff_sends_nothing(self, active_domain: OrganizationDomain) -> None:
        notify_custom_domain_problem("00000000-0000-0000-0000-000000000000", "custom_domain_active_problem")
        notify_custom_domain_problem(str(active_domain.pk), "custom_domain_active_problem")

        assert mail.outbox == []
