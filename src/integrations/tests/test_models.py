"""Model invariants: uniqueness, effective auto-sync, error recording."""

import typing as t

import pytest
from django.db import IntegrityError

from events.models import Event, Organization, TicketTier
from integrations.models import EventLink, PlatformConnection, TierLink
from integrations.schema import ConnectionStatus, IntegrationErrorCode

pytestmark = pytest.mark.django_db


@pytest.fixture
def connection(organization: Organization) -> PlatformConnection:
    return PlatformConnection.objects.create(
        organization=organization,
        provider=PlatformConnection.Provider.EVENTBRITE,
        access_token="secret-token",
        remote_account_id="org-1",
        remote_account_name="Org One",
        status=PlatformConnection.Status.ACTIVE,
    )


def test_one_connection_per_provider_per_org(connection: PlatformConnection) -> None:
    # bulk_create skips TimeStampedModel.save/full_clean, so the duplicate hits the
    # DB UniqueConstraint directly and raises IntegrityError (not ValidationError).
    with pytest.raises(IntegrityError):
        PlatformConnection.objects.bulk_create(
            [
                PlatformConnection(
                    organization=connection.organization,
                    provider=PlatformConnection.Provider.EVENTBRITE,
                    access_token="x",
                )
            ]
        )


def test_token_is_stored_encrypted_and_readable(connection: PlatformConnection) -> None:
    connection.refresh_from_db()
    assert connection.token().access_token == "secret-token"
    # EncryptedTextField decrypts on read; the ciphertext lives in the DB column (checked via raw SQL).
    from django.db import connection as db

    with db.cursor() as cur:
        cur.execute("SELECT access_token FROM integrations_platformconnection WHERE id = %s", [connection.pk])
        assert cur.fetchone()[0] != "secret-token"


def test_webhook_secret_is_generated_and_unique(organization: Organization) -> None:
    a = PlatformConnection.objects.create(organization=organization, provider="eventbrite", access_token="a")
    assert len(a.webhook_secret) >= 40
    assert a.webhook_secret != PlatformConnection(access_token="b").webhook_secret


def test_record_error_sets_status_and_payload(connection: PlatformConnection) -> None:
    connection.record_error(
        IntegrationErrorCode.CONNECTION_REVOKED, "Reconnect", "401", status=PlatformConnection.Status.ERROR
    )
    connection.refresh_from_db()
    assert connection.status == PlatformConnection.Status.ERROR
    assert connection.last_error == {"code": "connection_revoked", "detail": "Reconnect", "provider_message": "401"}


def test_record_error_without_status_keeps_status(connection: PlatformConnection) -> None:
    """Test that calling record_error without status parameter leaves status unchanged."""
    assert connection.status == PlatformConnection.Status.ACTIVE
    connection.record_error(IntegrationErrorCode.WEBHOOK_REGISTRATION_FAILED, "Live updates unavailable")
    connection.refresh_from_db()
    assert connection.status == PlatformConnection.Status.ACTIVE
    assert connection.last_error == {
        "code": "webhook_registration_failed",
        "detail": "Live updates unavailable",
        "provider_message": None,
    }


def test_event_link_effective_auto_sync_inherits(connection: PlatformConnection, event: Event) -> None:
    link = EventLink.objects.create(event=event, connection=connection, remote_id="ev-1")
    assert link.effective_auto_sync is False
    connection.auto_sync = True
    connection.save(update_fields=["auto_sync"])
    link.refresh_from_db()
    assert link.effective_auto_sync is True
    # mypy narrows the `effective_auto_sync` property to Literal[True] from the assert above and
    # doesn't know this assignment to the underlying field changes its computed value.
    link.auto_sync = False  # type: ignore[unreachable]
    assert link.effective_auto_sync is False


def test_one_link_per_connection_per_event(connection: PlatformConnection, event: Event) -> None:
    EventLink.objects.create(event=event, connection=connection, remote_id="ev-1")
    # bulk_create skips TimeStampedModel.save/full_clean, so the duplicate hits the
    # DB UniqueConstraint directly and raises IntegrityError (not ValidationError).
    with pytest.raises(IntegrityError):
        EventLink.objects.bulk_create([EventLink(event=event, connection=connection, remote_id="ev-2")])


def test_one_remote_id_per_connection(connection: PlatformConnection, event: Event) -> None:
    """Two different events cannot both claim the same remote_id on one connection."""
    other_event = Event.objects.create(organization=event.organization, name="Other", start=event.start, end=event.end)
    EventLink.objects.create(event=event, connection=connection, remote_id="ev-1")
    # bulk_create skips TimeStampedModel.save/full_clean, so the duplicate hits the
    # DB UniqueConstraint directly and raises IntegrityError (not ValidationError).
    with pytest.raises(IntegrityError):
        EventLink.objects.bulk_create([EventLink(event=other_event, connection=connection, remote_id="ev-1")])


def test_blank_remote_id_exempt_from_unique_remote_id_per_connection(
    connection: PlatformConnection, event: Event
) -> None:
    """Multiple pending (unpushed) links with remote_id="" are allowed on the same connection."""
    other_event = Event.objects.create(organization=event.organization, name="Other", start=event.start, end=event.end)
    EventLink.objects.create(event=event, connection=connection, remote_id="")
    EventLink.objects.bulk_create([EventLink(event=other_event, connection=connection, remote_id="")])
    assert EventLink.objects.filter(connection=connection, remote_id="").count() == 2


def test_tier_link_unique_per_event_link(connection: PlatformConnection, event: Event, ticket_tier: TicketTier) -> None:
    link = EventLink.objects.create(event=event, connection=connection, remote_id="ev-1")
    TierLink.objects.create(tier=ticket_tier, event_link=link, remote_id="tc-1")
    # bulk_create skips TimeStampedModel.save/full_clean, so the duplicate hits the
    # DB UniqueConstraint directly and raises IntegrityError (not ValidationError).
    with pytest.raises(IntegrityError):
        TierLink.objects.bulk_create([TierLink(tier=ticket_tier, event_link=link, remote_id="tc-2")])


def test_connection_status_literal_matches_model() -> None:
    """Verify that the ConnectionStatus schema literal mirrors the model enum values."""
    assert set(t.get_args(ConnectionStatus)) == set(PlatformConnection.Status.values)
