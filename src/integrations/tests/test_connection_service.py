"""Connect flow against the FakeProvider: start, complete, multi-account, disconnect, revoke."""

import pytest

from accounts.models import RevelUser
from events.models import Organization
from integrations.exceptions import IntegrationError, ProviderError
from integrations.models import PlatformConnection
from integrations.providers.base import RemoteAccount
from integrations.schema import IntegrationErrorCode
from integrations.service import connection_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


def test_begin_connect_returns_provider_url_with_state(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    assert start.state in start.authorize_url
    assert "redirect_uri=" in start.authorize_url
    assert connection_service.redirect_uri("fake").endswith("/api/integrations/fake/callback")


def test_complete_connect_single_account_activates_and_registers_webhook(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    conn = connection_service.complete_connect(start.state, code="c1")
    assert conn.status == PlatformConnection.Status.ACTIVE
    assert conn.remote_account_id == "acc-1"
    assert conn.remote_account_name == "Fake Org"
    assert conn.token().access_token == "tok-c1"
    assert conn.webhook_remote_id == "wh-1"
    assert fake_provider.webhooks["wh-1"].endswith(f"/api/integrations/fake/webhook/{conn.webhook_secret}")


def test_complete_connect_multiple_accounts_leaves_pending(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    fake_provider.accounts = [RemoteAccount(remote_id="a", name="A"), RemoteAccount(remote_id="b", name="B")]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    conn = connection_service.complete_connect(start.state, code="c2")
    assert conn.status == PlatformConnection.Status.PENDING
    assert conn.webhook_remote_id == ""
    accounts = connection_service.list_pending_accounts(organization, "fake")
    assert [a.remote_id for a in accounts] == ["a", "b"]
    conn = connection_service.select_account(organization, "fake", "b")
    assert (conn.status, conn.remote_account_name, conn.webhook_remote_id) == ("active", "B", "wh-1")


def test_select_unknown_account_rejected(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    fake_provider.accounts = [RemoteAccount(remote_id="a", name="A"), RemoteAccount(remote_id="b", name="B")]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    connection_service.complete_connect(start.state, code="c3")
    with pytest.raises(IntegrationError) as exc:
        connection_service.select_account(organization, "fake", "zzz")
    assert exc.value.code == IntegrationErrorCode.ACCOUNT_UNKNOWN


def test_complete_connect_rechecks_ownership(
    organization: Organization,
    organization_owner_user: RevelUser,
    fake_provider: FakeProvider,
    django_user_model: type[RevelUser],
) -> None:
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    organization.owner = django_user_model.objects.create_user(
        username="new_owner", email="n@example.com", password="p"
    )
    organization.save(update_fields=["owner"])
    with pytest.raises(IntegrationError) as exc:
        connection_service.complete_connect(start.state, code="c4")
    assert exc.value.code == IntegrationErrorCode.STATE_INVALID
    assert not PlatformConnection.objects.exists()


def test_begin_connect_refuses_when_already_active(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    connection_service.complete_connect(start.state, code="c5")
    with pytest.raises(IntegrationError) as exc:
        connection_service.begin_connect(organization, organization_owner_user, "fake")
    assert exc.value.code == IntegrationErrorCode.ALREADY_CONNECTED
    assert exc.value.status == 409


def test_reconnect_after_error_replaces_token(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    conn = connection_service.complete_connect(start.state, code="c6")
    connection_service.mark_revoked(conn)
    assert conn.status == PlatformConnection.Status.ERROR
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    conn = connection_service.complete_connect(start.state, code="c7")
    assert (conn.status, conn.token().access_token, conn.last_error) == ("active", "tok-c7", None)
    assert PlatformConnection.objects.count() == 1


def test_exchange_failure_maps_to_provider_rejected(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    fake_provider.fail_exchange = ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "invalid_grant")
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    with pytest.raises(IntegrationError) as exc:
        connection_service.complete_connect(start.state, code="bad")
    assert exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED
    assert exc.value.provider_message == "invalid_grant"
    assert exc.value.status == 502


def test_webhook_failure_does_not_block_connection(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    fake_provider.fail_webhook = ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "https required")
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    conn = connection_service.complete_connect(start.state, code="c8")
    assert conn.status == PlatformConnection.Status.ACTIVE
    assert conn.webhook_remote_id == ""
    assert conn.last_error is not None and conn.last_error["code"] == "webhook_registration_failed"


def test_disconnect_unregisters_webhook_and_revokes(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    conn = connection_service.complete_connect(start.state, code="c9")
    connection_service.disconnect(organization, "fake")
    assert fake_provider.webhooks == {}
    assert fake_provider.revoked == [conn.token().access_token]
    assert not PlatformConnection.objects.exists()


def test_list_pending_accounts_revoked_token_maps_to_connection_revoked(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    """A pending connection whose stored token the provider has since revoked surfaces ``CONNECTION_REVOKED``.

    ``FakeProvider.list_accounts`` is deterministic on the token value, so the ``tok-revoked``
    sentinel can't be reached via ``complete_connect`` itself (it calls ``list_accounts`` with
    the freshly exchanged token to decide single- vs multi-account binding, and would fail
    there first). Simulate the token going bad *after* the connection is left pending, by
    overwriting the stored token directly, then exercise the picker's ``list_pending_accounts``.
    """
    fake_provider.accounts = [RemoteAccount(remote_id="a", name="A"), RemoteAccount(remote_id="b", name="B")]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    conn = connection_service.complete_connect(start.state, code="c1")
    assert conn.status == PlatformConnection.Status.PENDING
    conn.access_token = "tok-revoked"
    conn.save(update_fields=["access_token"])
    with pytest.raises(IntegrationError) as exc:
        connection_service.list_pending_accounts(organization, "fake")
    assert exc.value.code == IntegrationErrorCode.CONNECTION_REVOKED
    assert exc.value.status == 400


def test_list_connections_one_row_per_enabled_provider(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> None:
    rows = connection_service.list_connections(organization)
    assert [(r.provider, r.status) for r in rows] == [("fake", None)]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    connection_service.complete_connect(start.state, code="c10")
    rows = connection_service.list_connections(organization)
    assert [(r.provider, r.status, r.remote_account_name) for r in rows] == [("fake", "active", "Fake Org")]
