"""Push/publish orchestration (spec §7.3–7.4). Function-based; the Celery tasks in ``integrations.tasks`` call in."""

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from events.models import Event, Organization, TicketTier
from integrations import registry
from integrations.exceptions import IntegrationError, ProviderError, RetryableProviderError
from integrations.models import EventLink, PlatformConnection, TierLink
from integrations.providers.base import ListingProvider, RemoteEventRef
from integrations.schema import IntegrationErrorCode, SyncReportEntry
from integrations.service import connection_service, mapper
from integrations.service.mapper import EventNotEligible

logger = structlog.get_logger(__name__)


def report_entry(
    code: IntegrationErrorCode, detail: str, provider_message: str | None = None, *, tier: TicketTier | None = None
) -> SyncReportEntry:
    """Build one report row."""
    return SyncReportEntry(
        scope="tier" if tier else "event",
        tier_id=tier.id if tier else None,
        tier_name=tier.name if tier else None,
        code=code,
        detail=detail,
        provider_message=provider_message,
    )


def get_link(event: Event, provider_key: str) -> EventLink | None:
    """The event's link for a provider, or None."""
    return EventLink.objects.select_related("connection").filter(event=event, connection__provider=provider_key).first()


def ensure_link(event: Event, connection: PlatformConnection) -> EventLink:
    """Get or create the (pending, unpushed) link for this event on this connection."""
    link, _created = EventLink.objects.get_or_create(
        event=event, connection=connection, defaults={"remote_id": "", "sync_state": EventLink.SyncState.PENDING}
    )
    return link


def _active_connection(organization: Organization, provider_key: str) -> PlatformConnection:
    """The org's ACTIVE connection for the provider (404 when absent, 409 when revoked/pending)."""
    conn = connection_service.get_connection(organization, provider_key)
    if conn.status != PlatformConnection.Status.ACTIVE:
        raise IntegrationError(
            IntegrationErrorCode.CONNECTION_REVOKED,
            str(_("The platform connection needs to be re-established.")),
            status=409,
        )
    return conn


def request_push(event: Event, provider_key: str) -> EventLink:
    """Controller entry point: validate, mark pending, schedule the push after commit."""
    try:
        mapper.check_eligible(event)
    except EventNotEligible as e:
        raise IntegrationError(e.code, e.detail) from e
    conn = _active_connection(event.organization, provider_key)
    link = ensure_link(event, conn)
    link.sync_state = EventLink.SyncState.PENDING
    link.save(update_fields=["sync_state", "updated_at"])
    from integrations.tasks import push_event_link

    link_id = str(link.id)
    transaction.on_commit(lambda: push_event_link.delay(link_id))
    return link


def _write_report(link: EventLink, entries: list[SyncReportEntry], state: str) -> EventLink:
    link.sync_report = [e.model_dump(mode="json") for e in entries]
    link.sync_state = state
    link.save(update_fields=["sync_report", "sync_state", "updated_at"])
    return link


def _reconcile_tiers(
    link: EventLink, provider: ListingProvider, mapped: mapper.MappedEvent, *, existed_before: bool
) -> None:
    """Upsert every mappable tier; remove or hide remote classes Revel no longer maps (spec §7.3 step 3)."""
    token = link.connection.token()
    links_by_tier = {tl.tier_id: tl for tl in TierLink.objects.filter(event_link=link)}
    # Remote classes present before this push (update path only); `quantity_sold` is the platform's own count.
    remote_before = (
        {c.remote_id: c for c in provider.get_event(token, link.remote_id).ticket_classes} if existed_before else {}
    )
    upserted: set[str] = set()
    for m in mapped.tiers:
        remote_id = provider.upsert_ticket_class(token, link.remote_id, m.remote)
        upserted.add(remote_id)
        tl = links_by_tier.get(m.tier.id)
        if tl is None:
            TierLink.objects.create(tier=m.tier, event_link=link, remote_id=remote_id)
        elif tl.remote_id != remote_id:
            tl.remote_id = remote_id
            tl.save(update_fields=["remote_id", "updated_at"])
    links_by_remote = {tl.remote_id: tl for tl in TierLink.objects.filter(event_link=link)}
    for stale_id, remote_class in remote_before.items():
        if stale_id is None or stale_id in upserted:
            continue
        # The tier was deleted in Revel (its TierLink cascaded) or became unmappable this push.
        tl = links_by_remote.get(stale_id)
        sold = max(remote_class.quantity_sold, tl.remote_quantity_sold if tl else 0)
        if sold == 0:
            provider.delete_ticket_class(token, link.remote_id, stale_id)
            if tl is not None:
                tl.delete()
        else:
            provider.set_ticket_class_paused(token, link.remote_id, stale_id, True)
            if tl is not None:
                tl.remote_paused = True
                tl.save(update_fields=["remote_paused", "updated_at"])


def _apply_status(link: EventLink, provider: ListingProvider, event: Event, report: list[SyncReportEntry]) -> None:
    token = link.connection.token()
    if link.remote_status != EventLink.RemoteStatus.LIVE:
        return  # first push and drafts stay drafts; publishing is an explicit action
    if event.status == Event.EventStatus.CANCELLED:
        provider.cancel_event(token, link.remote_id)
        link.remote_status = EventLink.RemoteStatus.CANCELLED
    elif event.status == Event.EventStatus.DRAFT:
        report.append(
            report_entry(
                IntegrationErrorCode.UNPUBLISH_REFUSED,
                str(
                    _("The listing is live and cannot be unpublished from here; cancel it or edit it on the platform.")
                ),
            )
        )


def push_link(link: EventLink) -> EventLink:
    """Full-state push. Safe to retry; writes only to integrations models (never to Event/TicketTier)."""
    event = link.event
    conn = link.connection
    provider = registry.get_provider(conn.provider)
    token = conn.token()
    existing_links = {tl.tier_id: tl for tl in TierLink.objects.filter(event_link=link)}
    mapped = mapper.map_event(
        event,
        remote_paused={tid: tl.remote_paused for tid, tl in existing_links.items()},
        remote_tier_ids={tid: tl.remote_id for tid, tl in existing_links.items()},
    )
    report = list(mapped.report)
    existed_before = bool(link.remote_id)
    try:
        if existed_before:
            try:
                ref: RemoteEventRef = provider.update_event(token, link.remote_id, mapped.remote)
            except ProviderError as e:
                if e.code != IntegrationErrorCode.REMOTE_EVENT_MISSING:
                    raise
                report.append(
                    report_entry(
                        e.code,
                        str(_("The listing no longer exists on the platform. Push again to recreate it.")),
                        e.provider_message,
                    )
                )
                link.remote_id = ""
                TierLink.objects.filter(event_link=link).delete()
                link.save(update_fields=["remote_id", "updated_at"])
                return _write_report(link, report, EventLink.SyncState.BROKEN)
        else:
            ref = provider.create_event(token, conn.remote_account_id, mapped.remote)
            link.remote_id, link.remote_url = ref.remote_id, ref.url
            link.remote_status = EventLink.RemoteStatus.DRAFT
            link.save(update_fields=["remote_id", "remote_url", "remote_status", "updated_at"])
        if mapped.remote.description_html:
            provider.set_description(token, link.remote_id, mapped.remote.description_html)
        _reconcile_tiers(link, provider, mapped, existed_before=existed_before)
        _apply_status(link, provider, event, report)
    except ProviderError as e:
        if e.retryable:
            raise RetryableProviderError(e.code, e.provider_message, retryable=True) from e
        if e.code == IntegrationErrorCode.CONNECTION_REVOKED:
            connection_service.mark_revoked(conn)
        report.append(report_entry(e.code, str(_("The platform rejected the update.")), e.provider_message))
        logger.warning("integration_push_failed", link_id=str(link.id), code=e.code.value)
        return _write_report(link, report, EventLink.SyncState.FAILED)
    link.last_pushed_at = timezone.now()
    link.save(update_fields=["last_pushed_at", "remote_status", "updated_at"])
    logger.info("integration_pushed", link_id=str(link.id), provider=conn.provider, remote_id=link.remote_id)
    return _write_report(link, report, EventLink.SyncState.IN_SYNC)
