"""Tests for custom-domain provisioning: request, status sync and deletion (#1151). Cloudflare is mocked."""

import typing as t
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.core.exceptions import ValidationError
from structlog.testing import capture_logs

from events.exceptions import CloudflareAPIError, CustomDomainConflictError, CustomDomainNotEnabledError
from events.models import Organization, OrganizationDomain, OrganizationDomainStatus
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import custom_domain_service, dns_diagnostics

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
CNAME_TARGET = "customers.letsrevel.io"


def hosted(
    status: str = "pending",
    ssl_status: str | None = "pending_validation",
    hostname: str = "events.example.com",
    cf_id: str = "cf-1",
    verification_errors: list[str] | None = None,
    ssl_errors: list[str] | None = None,
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
            validation_errors=[cloudflare.SslValidationError(message=m) for m in ssl_errors or []],
        )
        if ssl_status
        else None,
        ownership_verification=cloudflare.OwnershipVerification(
            type="txt", name="_cf-custom-hostname.events.example.com", value="owner-token"
        ),
        verification_errors=verification_errors or [],
    )


@pytest.fixture(autouse=True)
def platform_settings(settings: t.Any) -> None:
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False
    settings.CUSTOM_DOMAIN_CNAME_TARGET = CNAME_TARGET
    settings.CLOUDFLARE_API_TOKEN = ""
    settings.CLOUDFLARE_ZONE_ID = ""


@pytest.fixture(autouse=True)
def txt_published() -> t.Iterator[MagicMock]:
    """The ownership TXT record is published in public DNS (no network); tests override ``return_value``."""
    with patch.object(cloudflare, "txt_record_published", return_value=True) as mock:
        yield mock


@pytest.fixture
def configured(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ZONE_ID = "zone"


@pytest.fixture(autouse=True)
def no_dns_preflight() -> t.Iterator[MagicMock]:
    """The DNS pre-flight hints are tested in ``test_dns_diagnostics`` (no network); tests may set ``return_value``."""
    with patch.object(dns_diagnostics, "diagnose_dns", return_value=[]) as mock:
        yield mock


@pytest.fixture(autouse=True)
def no_dns_provider_lookup() -> t.Iterator[MagicMock]:
    """The DNS host detection is tested in ``test_dns_diagnostics`` (no network); tests may set ``return_value``."""
    with patch.object(dns_diagnostics, "detect_dns_provider", return_value=None) as mock:
        yield mock


@pytest.fixture
def enabled_org(organization: Organization) -> Organization:
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    return organization


def httpx_ok(result: t.Any) -> httpx.Response:
    """A Cloudflare ``success: true`` envelope around ``result``."""
    body = {"success": True, "errors": [], "messages": [], "result": result}
    return httpx.Response(200, json=body, request=httpx.Request("POST", "https://api.cloudflare.com/x"))


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


def test_request_warms_the_dns_provider_cache_for_the_hostname_before_any_lock(
    enabled_org: Organization, no_dns_provider_lookup: MagicMock
) -> None:
    """The response schema looks the provider up; doing it here keeps the DoH call out of the advisory lock."""
    custom_domain_service.request_domain(enabled_org, "  Events.Example.COM. ")

    no_dns_provider_lookup.assert_called_once_with("events.example.com")


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
    enabled_org: Organization, configured: None, existing: OrganizationDomainStatus
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


def test_unparseable_create_result_deletes_the_hostname_that_was_created(
    enabled_org: Organization, configured: None
) -> None:
    """Cloudflare created the hostname but its result had the wrong shape: delete it by the raw id."""
    bad_result = {"id": "cf-raw", "hostname": "events.example.com"}  # no ``status``
    ok = httpx_ok(bad_result)
    with (
        patch(
            "events.service.cloudflare_custom_hostnames.httpx.request", side_effect=[ok, httpx_ok({"id": "cf-raw"})]
        ) as request,
        pytest.raises(CloudflareAPIError),
    ):
        custom_domain_service.request_domain(enabled_org, "events.example.com")

    assert [c.args[0] for c in request.call_args_list] == ["POST", "DELETE"]
    assert request.call_args_list[1].args[1].endswith("/custom_hostnames/cf-raw")
    assert not OrganizationDomain.objects.filter(hostname="events.example.com").exists()


def test_unparseable_create_result_without_an_id_skips_cleanup(enabled_org: Organization, configured: None) -> None:
    with (
        patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=httpx_ok(None)) as request,
        pytest.raises(CloudflareAPIError),
    ):
        custom_domain_service.request_domain(enabled_org, "events.example.com")

    assert request.call_count == 1  # POST only


def test_unparseable_create_result_cleanup_failure_is_logged_not_raised(
    enabled_org: Organization, configured: None
) -> None:
    with (
        patch.object(
            cloudflare,
            "create_custom_hostname",
            side_effect=CloudflareAPIError("bad shape", hostname_id="cf-raw"),
        ),
        patch.object(cloudflare, "delete_custom_hostname", side_effect=CloudflareAPIError("nope")),
        capture_logs() as logs,
        pytest.raises(CloudflareAPIError, match="bad shape"),
    ):
        custom_domain_service.request_domain(enabled_org, "events.example.com")

    orphaned = [log for log in logs if log["event"] == "custom_domain_orphaned_cloudflare_hostname"]
    assert orphaned and orphaned[0]["cloudflare_hostname_id"] == "cf-raw"


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


TXT_NAME = "_cf-custom-hostname.events.example.com"
TXT_HINT = f"Add the TXT record {TXT_NAME} with value owner-token to prove you own this hostname."


def test_sync_checks_the_ownership_txt_record_before_activating(
    enabled_org: Organization, configured: None, txt_published: MagicMock
) -> None:
    domain = pending_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted("active", "active")):
        custom_domain_service.sync_domain_status(domain)

    txt_published.assert_called_once_with(TXT_NAME, "owner-token")
    domain.refresh_from_db()
    assert domain.status == Status.ACTIVE
    assert domain.last_errors == []


def test_sync_keeps_pending_and_says_what_to_add_when_the_txt_record_is_missing_or_wrong(
    enabled_org: Organization, configured: None, txt_published: MagicMock
) -> None:
    """A missing record and a wrong value both make ``txt_record_published`` answer False."""
    txt_published.return_value = False
    domain = pending_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted("active", "active")):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    enabled_org.refresh_from_db()
    assert domain.status == Status.PENDING
    assert domain.verified_at is None
    assert domain.last_errors == [TXT_HINT]
    assert enabled_org.custom_domain is None

    # Publishing the record lets the next sweep activate it and clears the hint.
    txt_published.return_value = True
    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted("active", "active")):
        custom_domain_service.sync_domain_status(domain)
    domain.refresh_from_db()
    assert domain.status == Status.ACTIVE
    assert domain.last_errors == []


def test_sync_keeps_pending_when_cloudflare_returns_no_ownership_record(
    enabled_org: Organization, configured: None, txt_published: MagicMock
) -> None:
    domain = pending_domain(enabled_org)
    result = hosted("active", "active").model_copy(update={"ownership_verification": None})

    with (
        patch.object(cloudflare, "get_custom_hostname", return_value=result),
        capture_logs() as logs,
    ):
        custom_domain_service.sync_domain_status(domain)

    txt_published.assert_not_called()
    domain.refresh_from_db()
    assert domain.status == Status.PENDING
    assert domain.last_errors == ["Cloudflare has not issued the ownership TXT record for this hostname yet."]
    warnings = [log for log in logs if log["event"] == "custom_domain_ownership_record_missing"]
    assert warnings and warnings[0]["log_level"] == "warning"


def ready_without_ownership() -> cloudflare.CustomHostname:
    """Cloudflare's answer once it has verified the hostname: active, and ``ownership_verification`` dropped."""
    return hosted("active", "active").model_copy(update={"ownership_verification": None})


def test_sync_activates_on_the_stored_ownership_record_once_cloudflare_drops_it(
    enabled_org: Organization, configured: None, txt_published: MagicMock
) -> None:
    """The order seen live: the request stores the TXT, then Cloudflare verifies and stops returning it."""
    with patch.object(cloudflare, "create_custom_hostname", return_value=hosted()):
        domain = custom_domain_service.request_domain(enabled_org, "events.example.com")

    with patch.object(cloudflare, "get_custom_hostname", return_value=ready_without_ownership()):
        custom_domain_service.sync_domain_status(domain)

    txt_published.assert_called_once_with(TXT_NAME, "owner-token")
    domain.refresh_from_db()
    assert domain.status == Status.ACTIVE
    assert domain.last_errors == []
    assert {"type": "TXT", "name": TXT_NAME, "value": "owner-token"} in domain.verification_records


def test_sync_keeps_listing_the_stored_ownership_record_while_it_is_not_published(
    enabled_org: Organization, configured: None, txt_published: MagicMock
) -> None:
    txt_published.return_value = False
    with patch.object(cloudflare, "create_custom_hostname", return_value=hosted()):
        domain = custom_domain_service.request_domain(enabled_org, "events.example.com")

    with patch.object(cloudflare, "get_custom_hostname", return_value=ready_without_ownership()):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.status == Status.PENDING
    assert domain.last_errors == [TXT_HINT]
    # The page keeps telling the owner which record to add, although Cloudflare no longer returns it.
    assert {"type": "TXT", "name": TXT_NAME, "value": "owner-token"} in domain.verification_records


def test_sync_falls_back_only_to_the_ownership_record_of_this_hostname(
    enabled_org: Organization, configured: None, txt_published: MagicMock
) -> None:
    """Other stored TXT records (the ACME challenge, another name) never stand in for the ownership proof."""
    domain = OrganizationDomain.objects.create(
        organization=enabled_org,
        hostname="events.example.com",
        cloudflare_hostname_id="cf-1",
        status=Status.PENDING,
        verification_records=[
            {"type": "TXT", "name": "_acme-challenge.events.example.com", "value": "acme-token"},
            {"type": "TXT", "name": "_cf-custom-hostname.other.example.com", "value": "other-token"},
        ],
    )

    with patch.object(cloudflare, "get_custom_hostname", return_value=ready_without_ownership()):
        custom_domain_service.sync_domain_status(domain)

    txt_published.assert_not_called()
    domain.refresh_from_db()
    assert domain.status == Status.PENDING
    assert domain.last_errors == ["Cloudflare has not issued the ownership TXT record for this hostname yet."]


def test_sync_does_not_recheck_ownership_for_an_active_domain(
    enabled_org: Organization, configured: None, txt_published: MagicMock
) -> None:
    txt_published.return_value = False  # the record may be removed once ownership was proven
    domain = active_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted("active", "active")):
        custom_domain_service.sync_domain_status(domain)

    txt_published.assert_not_called()
    domain.refresh_from_db()
    assert domain.status == Status.ACTIVE
    assert domain.last_errors == []


def test_sync_does_not_hand_a_dangling_cname_hostname_to_another_org(
    enabled_org: Organization, configured: None, txt_published: MagicMock, django_user_model: t.Any
) -> None:
    """Org A removed its domain but its CNAME still points at us; org B re-requests the hostname.

    Cloudflare validates over HTTP and reports active+active, but B cannot publish the TXT record in the
    zone's DNS, so B never gets the hostname.
    """
    org_a = Organization.objects.create(
        name="A", slug="org-a", owner=django_user_model.objects.create_user("a", "a@example.com", "pw")
    )
    org_a.custom_domain_enabled = True
    org_a.save(update_fields=["custom_domain_enabled"])
    custom_domain_service.remove_domain(pending_domain(org_a, cf_id="cf-a"))
    txt_published.return_value = False  # the zone's TXT for the new ownership token is not published

    with patch.object(cloudflare, "create_custom_hostname", return_value=hosted(cf_id="cf-b")):
        domain_b = custom_domain_service.request_domain(enabled_org, "events.example.com")
    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted("active", "active", cf_id="cf-b")):
        custom_domain_service.sync_domain_status(domain_b)

    domain_b.refresh_from_db()
    enabled_org.refresh_from_db()
    assert domain_b.status == Status.PENDING
    assert domain_b.last_errors == [TXT_HINT]
    assert enabled_org.custom_domain is None


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


@pytest.mark.parametrize("status", [Status.FAILED, Status.REMOVED])
def test_sync_skips_domains_without_a_cloudflare_id_or_not_live(
    enabled_org: Organization, configured: None, status: OrganizationDomainStatus
) -> None:
    manual = pending_domain(enabled_org, cf_id=None)

    with patch.object(cloudflare, "get_custom_hostname") as get:
        custom_domain_service.sync_domain_status(manual)
        manual.status = status
        manual.cloudflare_hostname_id = "cf-1"
        custom_domain_service.sync_domain_status(manual)

    get.assert_not_called()


def test_sync_is_a_no_op_when_cloudflare_is_not_configured(enabled_org: Organization) -> None:
    """**Check now** must not call Cloudflare with an empty token (it would answer 502)."""
    domain = pending_domain(enabled_org)  # has a cloudflare_hostname_id, but CLOUDFLARE_* is unset

    with patch.object(cloudflare, "get_custom_hostname") as get:
        result = custom_domain_service.sync_domain_status(domain)

    get.assert_not_called()
    assert result.status == Status.PENDING
    domain.refresh_from_db()
    assert domain.status == Status.PENDING


def test_sync_stores_cloudflare_problems_and_clears_them_when_resolved(
    enabled_org: Organization, configured: None
) -> None:
    domain = pending_domain(enabled_org)
    problems = hosted(
        verification_errors=["CNAME does not point to the fallback origin"],
        ssl_errors=["DCV failed", "CNAME does not point to the fallback origin"],
    )

    with patch.object(cloudflare, "get_custom_hostname", return_value=problems):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.status == Status.PENDING
    assert domain.last_errors == ["CNAME does not point to the fallback origin", "DCV failed"]  # de-duplicated

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted()):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.last_errors == []


def test_sync_reads_hostname_errors_without_an_ssl_object(enabled_org: Organization, configured: None) -> None:
    domain = pending_domain(enabled_org)

    with patch.object(
        cloudflare, "get_custom_hostname", return_value=hosted(ssl_status=None, verification_errors=["x"])
    ):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.last_errors == ["x"]


# ---- sync_domain_status: ACTIVE domains ----


def active_domain(org: Organization, cf_id: str = "cf-1") -> OrganizationDomain:
    domain: OrganizationDomain = pending_domain(org, cf_id=cf_id)
    custom_domain_service.activate_domain(domain)
    org.refresh_from_db()
    assert org.custom_domain == domain.hostname
    return domain


@pytest.mark.parametrize(
    ("status", "ssl_status"),
    [
        ("blocked", "active"),
        ("deleted", "active"),
        ("active", "deleted"),
    ],
)
def test_sync_fails_an_active_domain_on_a_terminal_cloudflare_state(
    enabled_org: Organization, configured: None, status: str, ssl_status: str
) -> None:
    domain = active_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted(status, ssl_status)):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    enabled_org.refresh_from_db()
    assert domain.status == Status.FAILED
    assert enabled_org.custom_domain is None


@pytest.mark.parametrize(
    ("status", "ssl_status", "expected_error"),
    [
        ("active", "validation_timed_out", "'validation_timed_out'"),
        ("active", "issuance_timed_out", "'issuance_timed_out'"),
        ("active", "initializing_timed_out", "'initializing_timed_out'"),
        ("active", "deployment_timed_out", "'deployment_timed_out'"),
    ],
)
def test_sync_keeps_an_active_domain_active_but_records_certificate_timeouts(
    enabled_org: Organization, configured: None, status: str, ssl_status: str, expected_error: str
) -> None:
    """A certificate timeout may be transient and DNS is still ours: an ACTIVE domain only records it."""
    domain = active_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted(status, ssl_status)):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    enabled_org.refresh_from_db()
    assert domain.status == Status.ACTIVE
    assert enabled_org.custom_domain == "events.example.com"
    assert any(expected_error in e for e in domain.last_errors), domain.last_errors


def test_sync_fails_an_active_domain_that_moved(enabled_org: Organization, configured: None) -> None:
    """``moved``: DNS no longer reaches us, so an ACTIVE domain fails at once and its hostname is withdrawn."""
    domain = active_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted("moved", "active")):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    enabled_org.refresh_from_db()
    assert domain.status == Status.FAILED
    assert enabled_org.custom_domain is None
    assert any("'moved'" in e for e in domain.last_errors), domain.last_errors


@pytest.mark.parametrize(
    ("status", "ssl_status"),
    [
        ("active", "active"),
        ("active", "active_redeploying"),
        ("active", "pending_validation"),
        ("pending", "pending_issuance"),
        ("active", "pending_deployment"),
        ("active", None),
    ],
)
def test_sync_keeps_an_active_domain_active_on_transient_states(
    enabled_org: Organization, configured: None, status: str, ssl_status: str | None
) -> None:
    domain = active_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted(status, ssl_status)):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    enabled_org.refresh_from_db()
    assert domain.status == Status.ACTIVE
    assert enabled_org.custom_domain == "events.example.com"


def test_sync_records_problems_on_an_active_domain_without_failing_it(
    enabled_org: Organization, configured: None
) -> None:
    domain = active_domain(enabled_org)

    with patch.object(
        cloudflare, "get_custom_hostname", return_value=hosted("active", "active_redeploying", ssl_errors=["renewing"])
    ):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.status == Status.ACTIVE
    assert domain.last_errors == ["renewing"]


def test_a_failed_active_domain_can_be_requested_again(enabled_org: Organization, configured: None) -> None:
    """FAILED (from an ACTIVE domain) is retired on the next request; REMOVED stays terminal."""
    domain = active_domain(enabled_org)
    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted("blocked", "active")):
        custom_domain_service.sync_domain_status(domain)

    with (
        patch.object(cloudflare, "delete_custom_hostname"),
        patch.object(cloudflare, "create_custom_hostname", return_value=hosted(cf_id="cf-new")),
    ):
        fresh = custom_domain_service.request_domain(enabled_org, "events.example.com")

    domain.refresh_from_db()
    assert domain.status == Status.REMOVED
    assert fresh.status == Status.PENDING


def test_sync_propagates_cloudflare_errors_and_leaves_the_domain_pending(
    enabled_org: Organization, configured: None
) -> None:
    domain = pending_domain(enabled_org)

    with patch.object(cloudflare, "get_custom_hostname", side_effect=CloudflareAPIError("boom")):
        with pytest.raises(CloudflareAPIError):
            custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.status == Status.PENDING


def test_sync_writes_records_with_a_queryset_update_not_save(enabled_org: Organization, configured: None) -> None:
    """``save()`` would run full_clean and the hostname validator; a legacy hostname must still sync."""
    domain = pending_domain(enabled_org)

    with (
        patch.object(cloudflare, "get_custom_hostname", return_value=hosted(verification_errors=["x"])),
        patch.object(OrganizationDomain, "save", side_effect=AssertionError("save() must not be used")),
        patch.object(OrganizationDomain, "full_clean", side_effect=AssertionError("full_clean must not run")),
    ):
        custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.last_errors == ["x"]
    assert domain.verification_records


def test_sync_does_not_resurrect_a_domain_removed_during_the_cloudflare_call(
    enabled_org: Organization, configured: None
) -> None:
    """Compare-and-set: Cloudflare said "failed" for a PENDING domain, but it was REMOVED meanwhile."""
    domain = pending_domain(enabled_org)

    def get_then_concurrent_delete(cf_id: str) -> cloudflare.CustomHostname:
        # A concurrent request removes the domain while this one is waiting on Cloudflare.
        custom_domain_service.remove_domain(OrganizationDomain.objects.get(pk=domain.pk))
        return hosted("blocked", "active", verification_errors=["late"])

    with (
        patch.object(cloudflare, "get_custom_hostname", side_effect=get_then_concurrent_delete),
        capture_logs() as logs,
    ):
        result = custom_domain_service.sync_domain_status(domain)

    domain.refresh_from_db()
    assert domain.status == Status.REMOVED
    assert domain.last_errors == []  # the stale decision wrote nothing
    assert result.status == Status.REMOVED
    skipped = [log for log in logs if log["event"] == "custom_domain_sync_transition_skipped"]
    assert skipped and skipped[0]["log_level"] == "info"
    assert skipped[0]["expected_status"] == Status.PENDING
    assert skipped[0]["current_status"] == Status.REMOVED


def test_sync_does_not_activate_a_domain_removed_during_the_cloudflare_call(
    enabled_org: Organization, configured: None
) -> None:
    domain = pending_domain(enabled_org)

    def get_then_concurrent_delete(cf_id: str) -> cloudflare.CustomHostname:
        custom_domain_service.remove_domain(OrganizationDomain.objects.get(pk=domain.pk))
        return hosted("active", "active")

    with patch.object(cloudflare, "get_custom_hostname", side_effect=get_then_concurrent_delete):
        result = custom_domain_service.sync_domain_status(domain)  # no "removed domain cannot be changed" error

    enabled_org.refresh_from_db()
    assert result.status == Status.REMOVED
    assert enabled_org.custom_domain is None


def test_sync_does_not_fail_a_domain_a_concurrent_sync_already_activated(
    enabled_org: Organization, configured: None
) -> None:
    """The decision was for PENDING; if the status is no longer PENDING, nothing is applied."""
    domain = pending_domain(enabled_org)

    def get_then_concurrent_activation(cf_id: str) -> cloudflare.CustomHostname:
        custom_domain_service.activate_domain(OrganizationDomain.objects.get(pk=domain.pk))
        return hosted("moved", "pending_validation")

    with patch.object(cloudflare, "get_custom_hostname", side_effect=get_then_concurrent_activation):
        result = custom_domain_service.sync_domain_status(domain)

    enabled_org.refresh_from_db()
    assert result.status == Status.ACTIVE
    assert enabled_org.custom_domain == "events.example.com"


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


def test_delete_warns_about_orphan_risk_when_cloudflare_is_not_configured(enabled_org: Organization) -> None:
    domain = pending_domain(enabled_org)  # has a Cloudflare id, but the platform has no Cloudflare config

    with patch.object(cloudflare, "delete_custom_hostname") as delete, capture_logs() as logs:
        custom_domain_service.delete_domain(domain)

    delete.assert_not_called()
    domain.refresh_from_db()
    assert domain.status == Status.REMOVED
    warnings = [e for e in logs if e["event"] == "custom_domain_cloudflare_not_configured_on_delete"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["cloudflare_hostname_id"] == "cf-1"


def test_delete_without_cloudflare_id_and_config_does_not_warn(enabled_org: Organization) -> None:
    domain = pending_domain(enabled_org, cf_id=None)

    with capture_logs() as logs:
        custom_domain_service.delete_domain(domain)

    assert not [e for e in logs if e["event"] == "custom_domain_cloudflare_not_configured_on_delete"]


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
