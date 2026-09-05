"""Import orchestration (spec §7.6): remote events listed, queued, and turned into Revel drafts."""

import typing as t
from functools import partial

import structlog
from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from markdownify import markdownify

from common.sanitizers import sanitize_html
from events.models import Event, Organization, TicketTier
from integrations import registry
from integrations.exceptions import IntegrationError, ProviderError
from integrations.models import EventLink, PlatformConnection, TierLink
from integrations.providers.base import RemoteEvent, RemoteTicketClass
from integrations.schema import (
    ImportResultSchema,
    IntegrationErrorCode,
    RemoteEventSummarySchema,
    SyncReportEntry,
)
from integrations.service import connection_service, mapper
from integrations.service.sync_service import active_connection

logger = structlog.get_logger(__name__)


def list_remote_events(organization: Organization, provider_key: str) -> list[RemoteEventSummarySchema]:
    """The remote account's events for the import picker, flagged when already linked."""
    conn = active_connection(organization, provider_key)
    provider = registry.get_provider(provider_key)
    try:
        summaries = provider.list_events(conn.token(), conn.remote_account_id)
    except ProviderError as e:
        if e.code == IntegrationErrorCode.CONNECTION_REVOKED:
            connection_service.mark_revoked(conn)
        raise IntegrationError(
            e.code, str(_("The platform could not list its events.")), e.provider_message, status=502
        ) from e
    linked = set(EventLink.objects.filter(connection=conn).values_list("remote_id", flat=True))
    return [
        RemoteEventSummarySchema(
            remote_id=s.remote_id,
            name=s.name,
            start=s.start,
            status=s.status,
            url=s.url,
            already_linked=s.remote_id in linked,
        )
        for s in summaries
    ]


def request_import(organization: Organization, provider_key: str, remote_ids: list[str]) -> ImportResultSchema:
    """Queue one import task per unlinked remote id."""
    conn = active_connection(organization, provider_key)
    linked = set(
        EventLink.objects.filter(connection=conn, remote_id__in=remote_ids).values_list("remote_id", flat=True)
    )
    queued = [rid for rid in dict.fromkeys(remote_ids) if rid not in linked]
    from integrations.tasks import import_remote_event as import_task

    conn_id = str(conn.id)
    for rid in queued:
        transaction.on_commit(partial(import_task.delay, conn_id, rid))
    return ImportResultSchema(queued=queued, skipped=[rid for rid in dict.fromkeys(remote_ids) if rid in linked])


def _tier_from_remote(event: Event, tc: RemoteTicketClass) -> TicketTier:
    """Create one draft ``TicketTier`` mirroring a remote ticket class.

    Raises:
        ValidationError: if the remote class maps to an invalid tier (e.g. a sales window that
            starts after the event) — the caller skips that class and reports it instead.
    """
    return TicketTier.objects.create(
        event=event,
        name=tc.name[:255],
        price=tc.price,
        currency=(tc.currency or str(settings.DEFAULT_CURRENCY))[:3],
        total_quantity=tc.quantity_total or None,
        sales_start_at=tc.sales_start,
        sales_end_at=tc.sales_end,
        visibility=TicketTier.Visibility.UNLISTED if tc.hidden else TicketTier.Visibility.PUBLIC,
        payment_method=TicketTier.PaymentMethod.FREE if tc.is_free else TicketTier.PaymentMethod.ONLINE,
        description=tc.description or None,
    )


def import_remote_event(connection: PlatformConnection, remote_id: str) -> EventLink:
    """Create a Revel draft from a remote event (spec §7.6). The link is written last on purpose.

    Invalid ticket classes are skipped individually (and noted in the link's ``sync_report``)
    rather than failing the whole import. A race between two callers importing the same
    ``remote_id`` is resolved by the connection+remote_id uniqueness constraint: the loser's
    draft is rolled back and it returns the winner's link.
    """
    existing = EventLink.objects.filter(connection=connection, remote_id=remote_id).first()
    if existing is not None:
        return existing
    provider = registry.get_provider(connection.provider)
    remote: RemoteEvent = provider.get_event(
        connection.token(), remote_id
    )  # ProviderError propagates → task fails loudly
    city = None
    location = None
    if remote.venue and remote.venue.latitude is not None and remote.venue.longitude is not None:
        location = Point(remote.venue.longitude, remote.venue.latitude, srid=4326)
        city = mapper.nearest_city(remote.venue.latitude, remote.venue.longitude)
    address = (remote.venue.address if remote.venue else "")[:255] or None
    try:
        with transaction.atomic():
            event = Event.objects.create(
                organization=connection.organization,
                name=remote.name[:255],
                description=markdownify(sanitize_html(remote.description_html)).strip() or None,
                status=Event.EventStatus.DRAFT,
                event_type=Event.EventType.PUBLIC,
                requires_ticket=True,
                start=remote.start,
                end=remote.end,
                is_virtual=remote.is_virtual,
                address=address,
                city=city,
                location=location,
            )
            event.ticket_tiers.all().delete()  # drop the signal-created default tier; remote classes are the truth
            tiers: list[tuple[TicketTier, RemoteTicketClass]] = []
            report: list[SyncReportEntry] = []
            for tc in remote.ticket_classes:
                try:
                    tiers.append((_tier_from_remote(event, tc), tc))
                except ValidationError as e:
                    logger.warning("integration_import_tier_skipped", remote_id=tc.remote_id, error=str(e))
                    report.append(
                        SyncReportEntry(
                            scope="tier",
                            tier_id=None,
                            tier_name=tc.name,
                            code=IntegrationErrorCode.PROVIDER_REJECTED,
                            detail=str(_("Ticket class %(name)s could not be imported.") % {"name": tc.name}),
                            provider_message=str(e),
                        )
                    )
            link = EventLink.objects.create(
                event=event,
                connection=connection,
                remote_id=remote_id,
                remote_url=remote.url,
                remote_status=remote.status,
                sync_state=EventLink.SyncState.IN_SYNC,
                origin=EventLink.Origin.IMPORTED,
                last_pulled_at=timezone.now(),
                sync_report=[entry.model_dump(mode="json") for entry in report],
            )
            TierLink.objects.bulk_create(
                [
                    TierLink(
                        tier=tier,
                        event_link=link,
                        remote_id=t.cast(str, tc.remote_id),
                        remote_quantity_sold=tc.quantity_sold,
                    )
                    for tier, tc in tiers
                ]
            )
    except IntegrityError, ValidationError:
        # A concurrent importer won the race on (connection, remote_id). Depending on timing
        # this surfaces as IntegrityError (raw INSERT conflict) or ValidationError
        # (TimeStampedModel.save's full_clean -> validate_constraints, when the winner's row
        # was already committed and visible before our own insert) — mirrors
        # common.utils.get_or_create_with_race_protection. Re-fetch the winner; if this wasn't
        # actually that race (no winner row appears), the failure is genuine — re-raise it.
        winner = EventLink.objects.filter(connection=connection, remote_id=remote_id).first()
        if winner is None:
            raise
        return winner
    logger.info("integration_imported", link_id=str(link.id), provider=connection.provider, remote_id=remote_id)
    return link
