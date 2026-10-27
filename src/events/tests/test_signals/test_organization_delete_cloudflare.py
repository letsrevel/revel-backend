"""Deleting an organization removes its Cloudflare custom hostnames (#1148 review, S4). Cloudflare is mocked."""

import typing as t
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from events.exceptions import CloudflareAPIError
from events.models import Organization, OrganizationDomain
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import custom_domain_service
from events.tasks import delete_organization_cloudflare_hostnames

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
DELAY = "events.tasks.custom_domains.delete_organization_cloudflare_hostnames.delay"


@pytest.fixture
def configured(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ZONE_ID = "zone"


def make_domain(org: Organization, hostname: str, cf_id: str | None, status: str = Status.ACTIVE) -> OrganizationDomain:
    return OrganizationDomain.objects.create(
        organization=org, hostname=hostname, cloudflare_hostname_id=cf_id, status=status
    )


class TestSignal:
    def test_deleting_an_organization_queues_its_hostname_deletion_on_commit(
        self, organization: Organization, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        make_domain(organization, "a.example.com", "cf-a")
        make_domain(organization, "b.example.com", "cf-b", status=Status.REMOVED)
        make_domain(organization, "c.example.com", None, status=Status.FAILED)  # never provisioned

        with patch(DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
            organization.delete()

        delay.assert_called_once()
        assert sorted(delay.call_args.args[0]) == ["cf-a", "cf-b"]
        assert not OrganizationDomain.objects.filter(hostname="a.example.com").exists()

    def test_nothing_is_queued_before_the_deletion_commits(
        self, organization: Organization, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        make_domain(organization, "a.example.com", "cf-a")

        with patch(DELAY) as delay, django_capture_on_commit_callbacks(execute=False):
            organization.delete()

        delay.assert_not_called()

    def test_organization_without_provisioned_domains_queues_nothing(
        self, organization: Organization, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        make_domain(organization, "c.example.com", None, status=Status.FAILED)

        with patch(DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
            organization.delete()

        delay.assert_not_called()


class TestDeleteOrphanedHostnames:
    def test_deletes_each_hostname_at_cloudflare(self, configured: None) -> None:
        with patch.object(cloudflare, "delete_custom_hostname") as delete:
            delete_organization_cloudflare_hostnames(["cf-a", "cf-b"])

        assert [c.args[0] for c in delete.call_args_list] == ["cf-a", "cf-b"]

    def test_not_found_counts_as_gone_and_other_errors_are_logged_not_raised(self, configured: None) -> None:
        def fake_delete(hostname_id: str) -> None:
            if hostname_id == "cf-gone":
                raise CloudflareAPIError("not found", status_code=404)
            if hostname_id == "cf-boom":
                raise CloudflareAPIError("server error", status_code=500)

        with (
            patch.object(cloudflare, "delete_custom_hostname", side_effect=fake_delete) as delete,
            capture_logs() as logs,
        ):
            custom_domain_service.delete_orphaned_cloudflare_hostnames(["cf-gone", "cf-boom", "cf-ok"])

        assert [c.args[0] for c in delete.call_args_list] == ["cf-gone", "cf-boom", "cf-ok"]  # none stops the rest
        orphaned = [log for log in logs if log["event"] == "custom_domain_orphaned_cloudflare_hostname"]
        assert [log["cloudflare_hostname_id"] for log in orphaned] == ["cf-boom"]

    def test_skipped_with_a_warning_when_cloudflare_is_not_configured(self, settings: t.Any) -> None:
        settings.CLOUDFLARE_API_TOKEN = ""
        settings.CLOUDFLARE_ZONE_ID = ""

        with patch.object(cloudflare, "delete_custom_hostname") as delete, capture_logs() as logs:
            custom_domain_service.delete_orphaned_cloudflare_hostnames(["cf-a"])

        delete.assert_not_called()
        assert any(log["event"] == "custom_domain_cloudflare_not_configured_on_organization_delete" for log in logs)

    def test_no_ids_is_a_no_op(self, configured: None) -> None:
        with patch.object(cloudflare, "delete_custom_hostname") as delete:
            custom_domain_service.delete_orphaned_cloudflare_hostnames([])

        delete.assert_not_called()
