"""The sweep re-asserts ``Organization.custom_domain`` against the ACTIVE domains (#1199).

The column is denormalized and written with queryset updates; a full ``save()`` of a stale ``Organization``
writes the old value back. These tests stage such drift with queryset updates and check that a sweep repairs it.
"""

import typing as t
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from structlog.testing import capture_logs

from events.models import Organization, OrganizationDomain, OrganizationDomainStatus
from events.service import custom_domain_service
from events.tasks import sync_custom_domains_task

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
REPAIRED_EVENT = "custom_domain_denormalized_hostname_repaired"


@pytest.fixture(autouse=True)
def no_cloudflare(settings: t.Any) -> None:
    """The heal must not depend on Cloudflare: none is configured, so the sweep only runs its local steps."""
    settings.CLOUDFLARE_API_TOKEN = ""
    settings.CLOUDFLARE_ZONE_ID = ""


@pytest.fixture
def invalidated() -> t.Iterator[t.Any]:
    with patch.object(custom_domain_service, "invalidate_tenant_cache") as mock:
        yield mock


@pytest.fixture
def make_domain() -> t.Callable[..., OrganizationDomain]:
    counter = iter(range(100))

    def _make(
        hostname: str,
        *,
        status: OrganizationDomainStatus = Status.ACTIVE,
        column: str | None = None,
        enabled: bool = True,
    ) -> OrganizationDomain:
        """A domain whose organization's ``custom_domain`` column is staged as ``column`` (``None``: unset)."""
        n = next(counter)
        owner = get_user_model().objects.create_user(f"owner{n}", f"owner{n}@example.com", "pw")
        org = Organization.objects.create(name=f"Org {n}", slug=f"org-{n}", owner=owner, custom_domain_enabled=enabled)
        Organization.objects.filter(pk=org.pk).update(custom_domain=column)
        return OrganizationDomain.objects.create(organization=org, hostname=hostname, status=status)

    return _make


def column_of(domain: OrganizationDomain) -> str | None:
    domain.organization.refresh_from_db()
    return domain.organization.custom_domain


def sweep(callbacks: t.Any) -> None:
    with callbacks(execute=True):
        sync_custom_domains_task()


def test_an_active_domain_whose_organization_lost_its_hostname_is_repaired(
    make_domain: t.Callable[..., OrganizationDomain], invalidated: t.Any, django_capture_on_commit_callbacks: t.Any
) -> None:
    domain = make_domain("live.example.com", column=None)

    with capture_logs() as logs:
        sweep(django_capture_on_commit_callbacks)

    assert column_of(domain) == "live.example.com"
    invalidated.assert_called_once_with("live.example.com")
    (warning,) = [log for log in logs if log["event"] == REPAIRED_EVENT]
    assert warning["log_level"] == "warning"
    assert warning["organization_id"] == str(domain.organization_id)
    assert warning["domain_id"] == str(domain.pk)


def test_a_wrong_value_is_replaced_and_both_hostnames_are_invalidated(
    make_domain: t.Callable[..., OrganizationDomain], invalidated: t.Any, django_capture_on_commit_callbacks: t.Any
) -> None:
    domain = make_domain("live.example.com", column="old.example.com")

    sweep(django_capture_on_commit_callbacks)

    assert column_of(domain) == "live.example.com"
    assert {call.args[0] for call in invalidated.call_args_list} == {"live.example.com", "old.example.com"}


@pytest.mark.parametrize("status", [Status.FAILED, Status.REMOVED, Status.PENDING])
def test_a_hostname_without_an_active_domain_is_cleared(
    status: OrganizationDomainStatus,
    make_domain: t.Callable[..., OrganizationDomain],
    invalidated: t.Any,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    domain = make_domain("dead.example.com", status=status, column="dead.example.com")

    with capture_logs() as logs:
        sweep(django_capture_on_commit_callbacks)

    assert column_of(domain) is None
    invalidated.assert_called_once_with("dead.example.com")
    (warning,) = [log for log in logs if log["event"] == REPAIRED_EVENT]
    assert warning["action"] == "cleared"


def test_a_hostname_with_no_domain_row_at_all_is_cleared(
    make_domain: t.Callable[..., OrganizationDomain], invalidated: t.Any, django_capture_on_commit_callbacks: t.Any
) -> None:
    domain = make_domain("dead.example.com", status=Status.REMOVED, column="dead.example.com")
    OrganizationDomain.objects.filter(pk=domain.pk).delete()

    sweep(django_capture_on_commit_callbacks)

    assert column_of(domain) is None


def test_a_hostname_moves_from_a_wrong_holder_to_its_active_domain(
    make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
) -> None:
    """``custom_domain`` is unique: the stale holder must be cleared before the right organization gets it."""
    rightful = make_domain("shared.example.com", column=None)
    squatter = make_domain("other.example.com", status=Status.FAILED, column="shared.example.com")

    sweep(django_capture_on_commit_callbacks)

    assert (column_of(rightful), column_of(squatter)) == ("shared.example.com", None)


def test_the_repair_happens_even_with_the_kill_switch_off(
    make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
) -> None:
    """The stored hostname survives the switch (flipping back restores the domain), so it must stay right."""
    domain = make_domain("live.example.com", column=None, enabled=False)

    sweep(django_capture_on_commit_callbacks)

    assert column_of(domain) == "live.example.com"


def test_consistent_rows_are_untouched_and_cost_two_queries(
    make_domain: t.Callable[..., OrganizationDomain],
    invalidated: t.Any,
    django_assert_num_queries: t.Any,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    domains = [make_domain(f"live{n}.example.com", column=f"live{n}.example.com") for n in range(4)]
    make_domain("failed.example.com", status=Status.FAILED, column=None)

    with capture_logs() as logs, django_assert_num_queries(2), django_capture_on_commit_callbacks(execute=True):
        repaired = custom_domain_service.heal_denormalized_hostnames()

    assert repaired == 0
    assert [column_of(d) for d in domains] == [f"live{n}.example.com" for n in range(4)]
    invalidated.assert_not_called()
    assert not [log for log in logs if log["event"] == REPAIRED_EVENT]


def test_a_failing_repair_does_not_stop_the_others(
    make_domain: t.Callable[..., OrganizationDomain], django_capture_on_commit_callbacks: t.Any
) -> None:
    broken = make_domain("broken.example.com", column=None)
    other = make_domain("other.example.com", column=None)
    real = custom_domain_service._heal_organization

    def fake(organization_id: t.Any) -> bool:
        if organization_id == broken.organization_id:
            raise RuntimeError("boom")
        return real(organization_id)

    with patch.object(custom_domain_service, "_heal_organization", side_effect=fake):
        sweep(django_capture_on_commit_callbacks)

    assert (column_of(broken), column_of(other)) == (None, "other.example.com")
