"""Tests for custom-domain provisioning: request, status sync and deletion (#1151). Cloudflare is mocked."""

import typing as t
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from events.exceptions import CloudflareAPIError, CustomDomainConflictError, CustomDomainNotEnabledError
from events.models import Organization, OrganizationDomain
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import custom_domain_service

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
CNAME_TARGET = "customers.letsrevel.io"


def hosted(
    status: str = "pending",
    ssl_status: str | None = "pending_validation",
    hostname: str = "events.example.com",
    cf_id: str = "cf-1",
) -> cloudflare.CustomHostname:
    """A Cloudflare custom hostname as the API would return it."""
    return cloudflare.CustomHostname(
        id=cf_id,
        hostname=hostname,
        status=status,
        ssl=cloudflare.CustomHostnameSsl(
            status=ssl_status,
            validation_records=[
                cloudflare.ValidationRecord(txt_name="_acme-challenge.events.example.com", txt_value="acme-token")
            ],
        )
        if ssl_status
        else None,
        ownership_verification=cloudflare.OwnershipVerification(
            type="txt", name="_cf-custom-hostname.events.example.com", value="owner-token"
        ),
    )


@pytest.fixture(autouse=True)
def platform_settings(settings: t.Any) -> None:
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False
    settings.CUSTOM_DOMAIN_CNAME_TARGET = CNAME_TARGET
    settings.CLOUDFLARE_API_TOKEN = ""
    settings.CLOUDFLARE_ZONE_ID = ""


@pytest.fixture
def configured(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ZONE_ID = "zone"


@pytest.fixture
def enabled_org(organization: Organization) -> Organization:
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    return organization


def pending_domain(org: Organization, cf_id: str | None = "cf-1", hostname: str = "events.example.com") -> t.Any:
    return OrganizationDomain.objects.create(
        organization=org, hostname=hostname, cloudflare_hostname_id=cf_id, status=Status.PENDING
    )


# ---- request_domain ----


def test_request_configured_creates_cloudflare_hostname_and_stores_records(
    enabled_org: Organization, configured: None
) -> None:
    with patch.object(cloudflare, "create_custom_hostname", return_value=hosted()) as create:
        domain = custom_domain_service.request_domain(enabled_org, "  Events.Example.COM. ")

    create.assert_called_once_with("events.example.com")
    domain.refresh_from_db()
    assert domain.status == Status.PENDING
    assert domain.hostname == "events.example.com"
    assert domain.cloudflare_hostname_id == "cf-1"
    assert domain.verification_records == [
        {"type": "CNAME", "name": "events.example.com", "value": CNAME_TARGET},
        {"type": "TXT", "name": "_cf-custom-hostname.events.example.com", "value": "owner-token"},
        {"type": "TXT", "name": "_acme-challenge.events.example.com", "value": "acme-token"},
    ]


def test_request_not_configured_stores_only_the_cname_and_skips_cloudflare(enabled_org: Organization) -> None:
    with patch.object(cloudflare, "create_custom_hostname") as create:
        domain = custom_domain_service.request_domain(enabled_org, "events.example.com")

    create.assert_not_called()
    assert domain.status == Status.PENDING
    assert domain.cloudflare_hostname_id is None
    assert domain.verification_records == [{"type": "CNAME", "name": "events.example.com", "value": CNAME_TARGET}]


def test_request_without_cname_target_setting_stores_no_instruction(enabled_org: Organization, settings: t.Any) -> None:
    settings.CUSTOM_DOMAIN_CNAME_TARGET = ""
    domain = custom_domain_service.request_domain(enabled_org, "events.example.com")
    assert domain.verification_records == []


def test_request_requires_the_platform_switch(organization: Organization, configured: None) -> None:
    with patch.object(cloudflare, "create_custom_hostname") as create:
        with pytest.raises(CustomDomainNotEnabledError):
            custom_domain_service.request_domain(organization, "events.example.com")

    create.assert_not_called()
    assert not OrganizationDomain.objects.exists()


@pytest.mark.parametrize("existing", [Status.PENDING, Status.ACTIVE])
def test_request_rejects_when_a_live_domain_exists(
    enabled_org: Organization, configured: None, existing: OrganizationDomain.Status
) -> None:
    OrganizationDomain.objects.create(organization=enabled_org, hostname="old.example.com", status=existing)

    with patch.object(cloudflare, "create_custom_hostname") as create:
        with pytest.raises(CustomDomainConflictError):
            custom_domain_service.request_domain(enabled_org, "events.example.com")

    create.assert_not_called()


def test_request_rejects_a_hostname_in_use_by_another_org(
    enabled_org: Organization, configured: None, django_user_model: t.Any
) -> None:
    other = Organization.objects.create(
        name="Other", slug="other", owner=django_user_model.objects.create_user("other", "o@example.com", "pw")
    )
    pending_domain(other, cf_id="cf-other")

    with patch.object(cloudflare, "create_custom_hostname") as create:
        with pytest.raises(CustomDomainConflictError):
            custom_domain_service.request_domain(enabled_org, "events.example.com")

    create.assert_not_called()


@pytest.mark.parametrize("hostname", ["letsrevel.io", "app.letsrevel.io", "1.2.3.4", "*.example.com", "nodots"])
def test_request_rejects_invalid_hostnames_before_touching_cloudflare(
    enabled_org: Organization, configured: None, hostname: str
) -> None:
    with patch.object(cloudflare, "create_custom_hostname") as create:
        with pytest.raises(ValidationError):
            custom_domain_service.request_domain(enabled_org, hostname)

    create.assert_not_called()
    assert not OrganizationDomain.objects.exists()


def test_cloudflare_failure_leaves_no_orphan_row(enabled_org: Organization, configured: None) -> None:
    with patch.object(cloudflare, "create_custom_hostname", side_effect=CloudflareAPIError("boom", status_code=500)):
        with pytest.raises(CloudflareAPIError):
            custom_domain_service.request_domain(enabled_org, "events.example.com")

    assert not OrganizationDomain.objects.exists()


def test_cloudflare_duplicate_hostname_is_a_conflict(enabled_org: Organization, configured: None) -> None:
    error = CloudflareAPIError("dup", status_code=409, error_codes=(1406,))
    with patch.object(cloudflare, "create_custom_hostname", side_effect=error):
        with pytest.raises(CustomDomainConflictError):
            custom_domain_service.request_domain(enabled_org, "events.example.com")

    assert not OrganizationDomain.objects.exists()


def test_failed_insert_deletes_the_just_created_cloudflare_hostname(
    enabled_org: Organization, configured: None
) -> None:
    with (
        patch.object(cloudflare, "create_custom_hostname", return_value=hosted()),
        patch.object(cloudflare, "delete_custom_hostname") as delete,
        patch.object(OrganizationDomain.objects, "create", side_effect=RuntimeError("db down")),
    ):
        with pytest.raises(RuntimeError):
            custom_domain_service.request_domain(enabled_org, "events.example.com")

    delete.assert_called_once_with("cf-1")


def test_failed_insert_cleanup_error_does_not_mask_the_original(enabled_org: Organization, configured: None) -> None:
    with (
        patch.object(cloudflare, "create_custom_hostname", return_value=hosted()),
        patch.object(cloudflare, "delete_custom_hostname", side_effect=CloudflareAPIError("nope")),
        patch.object(OrganizationDomain.objects, "create", side_effect=RuntimeError("db down")),
    ):
        with pytest.raises(RuntimeError, match="db down"):
            custom_domain_service.request_domain(enabled_org, "events.example.com")


def test_request_retires_a_failed_domain_of_the_same_org(enabled_org: Organization, configured: None) -> None:
    failed = OrganizationDomain.objects.create(
        organization=enabled_org, hostname="events.example.com", cloudflare_hostname_id="cf-old", status=Status.FAILED
    )

    with (
        patch.object(cloudflare, "delete_custom_hostname") as delete,
        patch.object(cloudflare, "create_custom_hostname", return_value=hosted(cf_id="cf-new")),
    ):
        domain = custom_domain_service.request_domain(enabled_org, "events.example.com")

    delete.assert_called_once_with("cf-old")
    failed.refresh_from_db()
    assert failed.status == Status.REMOVED
    assert domain.cloudflare_hostname_id == "cf-new"


# ---- get_current_domain ----


def test_get_current_domain_requires_the_switch(organization: Organization) -> None:
    with pytest.raises(CustomDomainNotEnabledError):
        custom_domain_service.get_current_domain(organization)


def test_get_current_domain_ignores_removed_domains(enabled_org: Organization) -> None:
    OrganizationDomain.objects.create(organization=enabled_org, hostname="gone.example.com", status=Status.REMOVED)
    assert custom_domain_service.get_current_domain(enabled_org) is None

    domain = pending_domain(enabled_org)
    assert custom_domain_service.get_current_domain(enabled_org) == domain


# ---- sync_domain_status ----


def test_sync_activates_when_hostname_and_certificate_are_active(enabled_org: Organization, configured: None) -> None:
    domain = pending_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted("active", "active")):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    enabled_org.refresh_from_db()
    assert domain.status == Status.ACTIVE
    assert domain.verified_at is not None
    assert enabled_org.custom_domain == "events.example.com"


@pytest.mark.parametrize(
    ("status", "ssl_status"),
    [("pending", "pending_validation"), ("active", "pending_issuance"), ("pending", "active"), ("active", None)],
)
def test_sync_keeps_pending_until_both_are_active(
    enabled_org: Organization, configured: None, status: str, ssl_status: str | None
) -> None:
    domain = pending_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted(status, ssl_status)):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.status == Status.PENDING


def test_sync_refreshes_the_verification_records(enabled_org: Organization, configured: None) -> None:
    domain = pending_domain(enabled_org)
    assert domain.verification_records == []

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted()):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.verification_records[-1] == {
        "type": "TXT",
        "name": "_acme-challenge.events.example.com",
        "value": "acme-token",
    }


@pytest.mark.parametrize(
    ("status", "ssl_status"),
    [
        ("blocked", "pending_validation"),
        ("moved", "active"),
        ("deleted", "deleted"),
        ("pending", "validation_timed_out"),
        ("pending", "issuance_timed_out"),
    ],
)
def test_sync_marks_terminal_states_failed(
    enabled_org: Organization, configured: None, status: str, ssl_status: str
) -> None:
    domain = pending_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted(status, ssl_status)):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.status == Status.FAILED


def test_sync_skips_domains_without_a_cloudflare_id_or_not_pending(enabled_org: Organization, configured: None) -> None:
    manual = pending_domain(enabled_org, cf_id=None)

    with patch.object(cloudflare, "get_custom_hostname") as get:
        custom_domain_service.sync_domain_status(manual)
        manual.status = Status.ACTIVE
        manual.cloudflare_hostname_id = "cf-1"
        custom_domain_service.sync_domain_status(manual)

    get.assert_not_called()


def test_sync_propagates_cloudflare_errors_and_leaves_the_domain_pending(
    enabled_org: Organization, configured: None
) -> None:
    domain = pending_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", side_effect=CloudflareAPIError("boom")):
        with pytest.raises(CloudflareAPIError):
            custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.status == Status.PENDING


# ---- delete_domain ----


def test_delete_removes_the_cloudflare_hostname_then_the_domain(enabled_org: Organization, configured: None) -> None:
    domain = pending_domain(enabled_org)

    with patch.object(cloudflare, "delete_custom_hostname") as delete:
        custom_domain_service.delete_domain(domain)

    delete.assert_called_once_with("cf-1")
    domain.refresh_from_db()
    assert domain.status == Status.REMOVED


def test_delete_without_cloudflare_id_skips_cloudflare(enabled_org: Organization, configured: None) -> None:
    domain = pending_domain(enabled_org, cf_id=None)

    with patch.object(cloudflare, "delete_custom_hostname") as delete:
        custom_domain_service.delete_domain(domain)

    delete.assert_not_called()
    domain.refresh_from_db()
    assert domain.status == Status.REMOVED


def test_delete_treats_cloudflare_404_as_already_gone(enabled_org: Organization, configured: None) -> None:
    domain = pending_domain(enabled_org)

    with patch.object(cloudflare, "delete_custom_hostname", side_effect=CloudflareAPIError("nf", status_code=404)):
        custom_domain_service.delete_domain(domain)

    domain.refresh_from_db()
    assert domain.status == Status.REMOVED


def test_delete_other_cloudflare_errors_keep_the_domain(enabled_org: Organization, configured: None) -> None:
    domain = pending_domain(enabled_org)

    with patch.object(cloudflare, "delete_custom_hostname", side_effect=CloudflareAPIError("x", status_code=500)):
        with pytest.raises(CloudflareAPIError):
            custom_domain_service.delete_domain(domain)

    domain.refresh_from_db()
    assert domain.status == Status.PENDING


def test_delete_withdraws_an_active_domain_from_the_organization(enabled_org: Organization) -> None:
    domain = pending_domain(enabled_org, cf_id=None)
    custom_domain_service.activate_domain(domain)

    custom_domain_service.delete_domain(domain)

    enabled_org.refresh_from_db()
    assert enabled_org.custom_domain is None
