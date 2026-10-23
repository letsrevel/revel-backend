"""Tests for the Turnstile hostname reconciliation, its triggers and the domain limit (#1148). Cloudflare is mocked."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.contrib import admin
from django.test import RequestFactory
from structlog.testing import capture_logs

from accounts.models import RevelUser
from events.admin.organization import OrganizationAdmin
from events.exceptions import CloudflareAPIError, CustomDomainConflictError
from events.models import Organization, OrganizationDomain
from events.service import cloudflare_turnstile, custom_domain_service, turnstile_hostname_service
from events.tasks import custom_domains as tasks
from events.tasks import notify_turnstile_sync_problem, reconcile_turnstile_hostnames, sync_custom_domains_task

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
RECONCILE_DELAY = "events.tasks.custom_domains.reconcile_turnstile_hostnames.delay"
NOTIFY_DELAY = "events.tasks.custom_domains.notify_turnstile_sync_problem.delay"


def widget(*domains: str) -> cloudflare_turnstile.TurnstileWidget:
    return cloudflare_turnstile.TurnstileWidget(name="Revel", mode="managed", domains=list(domains))


@pytest.fixture(autouse=True)
def turnstile_settings(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ACCOUNT_ID = "acct1"
    settings.CLOUDFLARE_ZONE_ID = ""  # Cloudflare for SaaS off: the Turnstile automation stands on its own
    settings.TURNSTILE_SITE_KEY = "0xSITE"
    settings.TURNSTILE_WIDGET_HOSTNAME_LIMIT = 10
    settings.CUSTOM_DOMAIN_LIMIT = 0


@pytest.fixture
def get_widget() -> t.Iterator[MagicMock]:
    with patch.object(cloudflare_turnstile, "get_widget", return_value=widget("letsrevel.io")) as mock:
        yield mock


@pytest.fixture
def set_domains() -> t.Iterator[MagicMock]:
    with patch.object(cloudflare_turnstile, "set_widget_domains") as mock:
        yield mock


@pytest.fixture
def make_domain(django_user_model: t.Any) -> t.Callable[..., OrganizationDomain]:
    counter = iter(range(1000))

    def _make(hostname: str, status: str = Status.ACTIVE, enabled: bool = True) -> OrganizationDomain:
        n = next(counter)
        owner = django_user_model.objects.create_user(f"o{n}", f"o{n}@example.com", "pw")
        org = Organization.objects.create(name=f"Org {n}", slug=f"org-{n}", owner=owner, custom_domain_enabled=enabled)
        return OrganizationDomain.objects.create(organization=org, hostname=hostname, status=status)

    return _make


def written_domains(set_domains: MagicMock) -> list[str]:
    return t.cast(list[str], set_domains.call_args.args[1])


class TestReconcile:
    def test_adds_active_domains_and_preserves_platform_entries(
        self, get_widget: MagicMock, set_domains: MagicMock, make_domain: t.Callable[..., OrganizationDomain]
    ) -> None:
        make_domain("a.example.org")
        make_domain("b.example.org")

        result = turnstile_hostname_service.reconcile_widget_hostnames()

        assert result == {"status": "updated", "overflow": 0}
        assert written_domains(set_domains) == ["letsrevel.io", "a.example.org", "b.example.org"]

    def test_removes_managed_hostnames_that_are_no_longer_active_or_enabled(
        self, get_widget: MagicMock, set_domains: MagicMock, make_domain: t.Callable[..., OrganizationDomain]
    ) -> None:
        make_domain("pending.example.org", Status.PENDING)
        make_domain("failed.example.org", Status.FAILED)
        make_domain("removed.example.org", Status.REMOVED)
        make_domain("off.example.org", enabled=False)
        make_domain("live.example.org")
        get_widget.return_value = widget(
            "letsrevel.io",
            "pending.example.org",
            "failed.example.org",
            "removed.example.org",
            "off.example.org",
            "live.example.org",
            "hand-added.example.net",  # not a Revel domain: never touched
        )

        turnstile_hostname_service.reconcile_widget_hostnames()

        assert written_domains(set_domains) == ["letsrevel.io", "live.example.org", "hand-added.example.net"]

    def test_no_put_when_nothing_changes(
        self, get_widget: MagicMock, set_domains: MagicMock, make_domain: t.Callable[..., OrganizationDomain]
    ) -> None:
        make_domain("a.example.org")
        make_domain("gone.example.org", Status.REMOVED)
        get_widget.return_value = widget("A.Example.org", "letsrevel.io")  # order and case do not matter

        result = turnstile_hostname_service.reconcile_widget_hostnames()

        assert result == {"status": "unchanged", "overflow": 0}
        set_domains.assert_not_called()

    def test_not_configured_is_a_no_op(
        self, settings: t.Any, get_widget: MagicMock, set_domains: MagicMock, make_domain: t.Callable[..., t.Any]
    ) -> None:
        make_domain("a.example.org")
        settings.CLOUDFLARE_ACCOUNT_ID = ""

        result = turnstile_hostname_service.reconcile_widget_hostnames()

        assert result == {"status": "disabled", "overflow": 0}
        get_widget.assert_not_called()
        set_domains.assert_not_called()

    @pytest.mark.parametrize("failing", ["get_widget", "set_widget_domains"])
    def test_cloudflare_error_is_logged_alerted_once_and_never_raised(
        self, failing: str, make_domain: t.Callable[..., t.Any]
    ) -> None:
        make_domain("a.example.org")
        error = CloudflareAPIError("boom", status_code=403)
        with (
            patch.object(cloudflare_turnstile, "get_widget", return_value=widget("letsrevel.io")),
            patch.object(cloudflare_turnstile, failing, side_effect=error),
            patch(NOTIFY_DELAY) as notify,
            capture_logs() as logs,
        ):
            first = turnstile_hostname_service.reconcile_widget_hostnames()
            second = turnstile_hostname_service.reconcile_widget_hostnames()

        assert first == second == {"status": "error", "overflow": 0}
        notify.assert_called_once_with("api_error", "boom")  # once per outage, not once per run
        assert [log["event"] for log in logs].count("turnstile_hostnames_sync_failed") == 2

    def test_alert_rearms_after_a_successful_run(self, get_widget: MagicMock, set_domains: MagicMock) -> None:
        with patch(NOTIFY_DELAY) as notify:
            get_widget.side_effect = [CloudflareAPIError("one"), widget("letsrevel.io"), CloudflareAPIError("two")]
            for _ in range(3):
                turnstile_hostname_service.reconcile_widget_hostnames()

        assert [call.args for call in notify.call_args_list] == [("api_error", "one"), ("api_error", "two")]

    def test_more_active_hostnames_than_the_widget_holds_writes_what_fits_and_alerts(
        self,
        settings: t.Any,
        get_widget: MagicMock,
        set_domains: MagicMock,
        make_domain: t.Callable[..., OrganizationDomain],
    ) -> None:
        settings.TURNSTILE_WIDGET_HOSTNAME_LIMIT = 3
        for name in ("a", "b", "c", "d"):
            make_domain(f"{name}.example.org")

        with patch(NOTIFY_DELAY) as notify:
            result = turnstile_hostname_service.reconcile_widget_hostnames()
            turnstile_hostname_service.reconcile_widget_hostnames()

        assert result == {"status": "updated", "overflow": 2}
        assert len(written_domains(set_domains)) == 3
        notify.assert_called_once()
        assert notify.call_args.args[0] == "over_capacity"

    def test_limit_zero_disables_the_capacity_check(
        self,
        settings: t.Any,
        get_widget: MagicMock,
        set_domains: MagicMock,
        make_domain: t.Callable[..., OrganizationDomain],
    ) -> None:
        settings.TURNSTILE_WIDGET_HOSTNAME_LIMIT = 0
        for n in range(15):
            make_domain(f"d{n}.example.org")

        result = turnstile_hostname_service.reconcile_widget_hostnames()

        assert result["overflow"] == 0
        assert len(written_domains(set_domains)) == 16


class TestSweepAndTasks:
    def test_the_sweep_reconciles_once_not_per_domain(
        self,
        settings: t.Any,
        get_widget: MagicMock,
        set_domains: MagicMock,
        make_domain: t.Callable[..., OrganizationDomain],
    ) -> None:
        settings.CLOUDFLARE_ZONE_ID = "zone"
        for n in range(3):
            domain = make_domain(f"d{n}.example.org")
            OrganizationDomain.objects.filter(pk=domain.pk).update(cloudflare_hostname_id=f"cf-{n}")

        with patch.object(custom_domain_service, "sync_domain_status") as sync:
            result = sync_custom_domains_task()

        assert result == {"synced": 3, "failed": 0}
        assert sync.call_count == 3
        get_widget.assert_called_once()
        set_domains.assert_called_once()

    def test_the_sweep_reconciles_without_cloudflare_for_saas(
        self, get_widget: MagicMock, set_domains: MagicMock, make_domain: t.Callable[..., t.Any]
    ) -> None:
        make_domain("a.example.org")

        assert sync_custom_domains_task() == {"synced": 0, "failed": 0}
        set_domains.assert_called_once()

    def test_a_crashing_reconcile_never_fails_the_sweep(self) -> None:
        with patch.object(turnstile_hostname_service, "reconcile_widget_hostnames", side_effect=RuntimeError("x")):
            assert sync_custom_domains_task() == {"synced": 0, "failed": 0}

    def test_reconcile_task_runs_the_reconciliation(self, get_widget: MagicMock, set_domains: MagicMock) -> None:
        assert reconcile_turnstile_hostnames()["status"] == "unchanged"

    def test_task_names_are_pinned(self) -> None:
        assert reconcile_turnstile_hostnames.name == "events.reconcile_turnstile_hostnames"
        assert notify_turnstile_sync_problem.name == "events.notify_turnstile_sync_problem"
        assert tasks.sync_custom_domains_task.name == "events.sync_custom_domains"

    @pytest.mark.parametrize(("kind", "needle"), [("api_error", "failed"), ("over_capacity", "full")])
    def test_alert_task_emails_staff(self, kind: str, needle: str, django_user_model: t.Any) -> None:
        django_user_model.objects.create_user("staff", "staff@example.com", "pw", is_staff=True)
        with patch.object(tasks, "send_email") as send:
            notify_turnstile_sync_problem(kind, "the detail")

        kwargs = send.call_args.kwargs
        assert kwargs["to"] == ["staff@example.com"]
        assert needle in kwargs["subject"]
        assert "the detail" in kwargs["body"]

    def test_alert_task_without_recipients_sends_nothing(self) -> None:
        RevelUser.objects.filter(is_staff=True).update(is_staff=False)
        RevelUser.objects.filter(is_superuser=True).update(is_superuser=False)
        with patch.object(tasks, "send_email") as send:
            notify_turnstile_sync_problem("api_error", "x")
        send.assert_not_called()


class TestDispatchOnCommit:
    """Every way a domain enters or leaves the widget list dispatches one reconcile, after commit."""

    def test_activation_dispatches_after_commit(
        self, make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        domain = make_domain("a.example.org", Status.PENDING)
        with patch(RECONCILE_DELAY) as delay:
            with django_capture_on_commit_callbacks(execute=False) as callbacks:
                custom_domain_service.activate_domain(domain)
            delay.assert_not_called()
            for callback in callbacks:
                callback()
        delay.assert_called_once()

    @pytest.mark.parametrize("transition", ["mark_domain_failed", "remove_domain"])
    def test_leaving_active_dispatches(
        self,
        transition: str,
        make_domain: t.Callable[..., OrganizationDomain],
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        domain = make_domain("a.example.org", Status.ACTIVE)
        with patch(RECONCILE_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
            getattr(custom_domain_service, transition)(domain)
        delay.assert_called_once()

    def test_a_pending_domain_failing_does_not_dispatch(
        self, make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        domain = make_domain("a.example.org", Status.PENDING)
        with patch(RECONCILE_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
            custom_domain_service.mark_domain_failed(domain)
        delay.assert_not_called()

    def test_nothing_is_dispatched_when_turnstile_automation_is_off(
        self,
        settings: t.Any,
        make_domain: t.Callable[..., OrganizationDomain],
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        settings.TURNSTILE_SITE_KEY = ""
        domain = make_domain("a.example.org", Status.PENDING)
        with patch(RECONCILE_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
            custom_domain_service.activate_domain(domain)
        delay.assert_not_called()

    def test_kill_switch_flip_dispatches(
        self, make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        domain = make_domain("a.example.org", Status.ACTIVE)
        org = Organization.objects.get(pk=domain.organization_id)
        org.custom_domain = "a.example.org"
        org.custom_domain_enabled = False
        form = MagicMock(changed_data=["custom_domain_enabled"])
        model_admin = OrganizationAdmin(Organization, admin.site)
        request = RequestFactory().post("/")
        with patch(RECONCILE_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
            model_admin.save_model(request, org, form, True)
        delay.assert_called_once()

    def test_organization_delete_dispatches(
        self, make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        domain = make_domain("a.example.org", Status.ACTIVE)
        org = Organization.objects.get(pk=domain.organization_id)
        org.custom_domain = "a.example.org"
        org.save(update_fields=["custom_domain"])
        with patch(RECONCILE_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
            org.delete()
        delay.assert_called_once()


class TestCustomDomainLimit:
    @pytest.fixture
    def enabled_org(self, organization: Organization) -> Organization:
        organization.custom_domain_enabled = True
        organization.save(update_fields=["custom_domain_enabled"])
        return organization

    def test_refuses_a_new_domain_at_the_limit(
        self, settings: t.Any, enabled_org: Organization, make_domain: t.Callable[..., OrganizationDomain]
    ) -> None:
        settings.CUSTOM_DOMAIN_LIMIT = 2
        make_domain("a.example.org", Status.ACTIVE)
        make_domain("b.example.org", Status.PENDING)

        with pytest.raises(CustomDomainConflictError, match="limit of custom domains"):
            custom_domain_service.request_domain(enabled_org, "c.example.org")

        assert not OrganizationDomain.objects.filter(hostname="c.example.org").exists()

    def test_allows_a_domain_below_the_limit(self, settings: t.Any, enabled_org: Organization) -> None:
        settings.CUSTOM_DOMAIN_LIMIT = 1

        domain = custom_domain_service.request_domain(enabled_org, "c.example.org")

        assert domain.status == Status.PENDING

    def test_zero_means_unlimited(
        self,
        settings: t.Any,
        enabled_org: Organization,
        make_domain: t.Callable[..., OrganizationDomain],
    ) -> None:
        settings.CUSTOM_DOMAIN_LIMIT = 0
        for n in range(12):
            make_domain(f"d{n}.example.org", Status.ACTIVE)

        assert custom_domain_service.request_domain(enabled_org, "c.example.org").hostname == "c.example.org"

    def test_failed_and_removed_domains_do_not_count(
        self, settings: t.Any, enabled_org: Organization, make_domain: t.Callable[..., OrganizationDomain]
    ) -> None:
        settings.CUSTOM_DOMAIN_LIMIT = 1
        make_domain("a.example.org", Status.FAILED)
        make_domain("b.example.org", Status.REMOVED)

        assert custom_domain_service.request_domain(enabled_org, "c.example.org").status == Status.PENDING

    def test_the_limit_is_rechecked_under_the_lock_and_the_cloudflare_hostname_is_cleaned_up(
        self, settings: t.Any, enabled_org: Organization, make_domain: t.Callable[..., OrganizationDomain]
    ) -> None:
        """A concurrent request that fills the last slot between the pre-check and the insert is still refused."""
        settings.CUSTOM_DOMAIN_LIMIT = 1
        settings.CLOUDFLARE_ZONE_ID = "zone"
        hosted = MagicMock(id="cf-new", ownership_verification=None, ssl=None)
        calls = iter([False, True])  # pre-check passes, the locked re-check sees the slot taken
        with (
            patch.object(custom_domain_service, "_domain_limit_reached", side_effect=lambda: next(calls)),
            patch.object(custom_domain_service, "_create_cloudflare_hostname", return_value=hosted),
            patch.object(custom_domain_service, "_delete_cloudflare_hostname_best_effort") as cleanup,
        ):
            with pytest.raises(CustomDomainConflictError):
                custom_domain_service.request_domain(enabled_org, "c.example.org")

        cleanup.assert_called_once_with("cf-new")
        assert not OrganizationDomain.objects.filter(hostname="c.example.org").exists()
