"""Push/publish orchestration (spec §7.3–7.4). The Celery tasks in ``integrations.tasks`` call in.

Importing remote events into Revel lives in ``integrations.service.import_service``.
"""

import typing as t

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from events.models import Event, Organization, TicketTier
from integrations import registry
from integrations.exceptions import IntegrationError, ProviderError, RetryableProviderError
from integrations.models import EventLink, PlatformConnection, TierLink
from integrations.providers.base import ListingProvider, RemoteEventRef
from integrations.schema import EventLinkSchema, IntegrationErrorCode, SyncReportEntry, TierLinkSchema
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


def active_connection(organization: Organization, provider_key: str) -> PlatformConnection:
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
    conn = active_connection(event.organization, provider_key)
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
    link: EventLink,
    provider: ListingProvider,
    mapped: mapper.MappedEvent,
    report: list[SyncReportEntry],
    *,
    existed_before: bool,
) -> None:
    """Upsert every mappable tier; remove or hide the remote classes *Revel created* (spec §7.3 step 3).

    Only classes carrying a ``TierLink`` are ever deleted or hidden: a class the organizer added
    on the platform itself is left alone and reported as ``remote_only_tier``.
    """
    token = link.connection.token()
    links_by_tier = {tl.tier_id: tl for tl in TierLink.objects.filter(event_link=link) if tl.tier_id is not None}
    # Remote classes present before this push (update path only); `quantity_sold` is the platform's own count.
    remote_before = (
        {c.remote_id: c for c in provider.get_event(token, link.remote_id).ticket_classes} if existed_before else {}
    )
    upserted: set[str] = set()
    for m in mapped.tiers:
        tl = links_by_tier.get(m.tier.id)
        remote = m.remote
        if existed_before and tl is not None and tl.remote_id not in remote_before:
            remote = remote.model_copy(update={"remote_id": None})  # deleted on the platform → recreate it
        remote_id = provider.upsert_ticket_class(token, link.remote_id, remote)
        upserted.add(remote_id)
        if tl is None:
            TierLink.objects.create(tier=m.tier, event_link=link, remote_id=remote_id)
        elif tl.remote_id != remote_id:
            tl.remote_id = remote_id
            tl.save(update_fields=["remote_id", "updated_at"])
    links_by_remote = {tl.remote_id: tl for tl in TierLink.objects.filter(event_link=link)}
    for stale_id, remote_class in remote_before.items():
        if stale_id is None or stale_id in upserted:
            continue
        tl = links_by_remote.get(stale_id)
        if tl is None:
            # Never Revel's: the organizer created this class on the platform. Hands off.
            report.append(
                SyncReportEntry(
                    scope="tier",
                    tier_id=None,
                    tier_name=remote_class.name,
                    code=IntegrationErrorCode.REMOTE_ONLY_TIER,
                    detail=str(_("This ticket class exists only on the platform and is left untouched.")),
                )
            )
            continue
        # The tier was deleted in Revel (its TierLink survives with tier=None) or became unmappable.
        if max(remote_class.quantity_sold, tl.remote_quantity_sold) == 0:
            provider.delete_ticket_class(token, link.remote_id, stale_id)
            tl.delete()
        elif not remote_class.hidden:
            # `remote_paused` stays untouched: it means "the organizer pressed pause", not this.
            provider.set_ticket_class_paused(token, link.remote_id, stale_id, True)


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
    try:
        mapper.check_eligible(event)
    except EventNotEligible as e:
        logger.info("integration_push_ineligible", link_id=str(link.id), code=e.code.value)
        return _write_report(link, [report_entry(e.code, e.detail)], EventLink.SyncState.FAILED)
    if link.remote_status == EventLink.RemoteStatus.CANCELLED:
        return _write_report(link, [], EventLink.SyncState.IN_SYNC)  # a cancelled listing is terminal
    existing_links = {tl.tier_id: tl for tl in TierLink.objects.filter(event_link=link) if tl.tier_id is not None}
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
        # Sent even when empty: clearing the description in Revel must clear it remotely too.
        provider.set_description(token, link.remote_id, mapped.remote.description_html)
        _reconcile_tiers(link, provider, mapped, report, existed_before=existed_before)
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


def note_retry(link: EventLink, error: ProviderError, *, exhausted: bool = False) -> EventLink:
    """Record a transient provider failure: ``pending`` while retries remain, ``failed`` once spent."""
    detail = (
        _("The platform kept refusing the push; try again later.")
        if exhausted
        else _("The platform is busy; the push will be retried shortly.")
    )
    state = EventLink.SyncState.FAILED if exhausted else EventLink.SyncState.PENDING
    entry = report_entry(IntegrationErrorCode.PROVIDER_RATE_LIMITED, str(detail), error.provider_message)
    return _write_report(link, [entry], state)


def note_failure(link: EventLink, error: Exception) -> EventLink:
    """Record an unexpected failure so the row never claims to be ``pending`` after the task died."""
    entry = report_entry(
        IntegrationErrorCode.PROVIDER_REJECTED, str(_("The push failed unexpectedly.")), str(error) or None
    )
    return _write_report(link, [entry], EventLink.SyncState.FAILED)


def to_link_schema(link: EventLink) -> EventLinkSchema:
    """Serialize a link with its tiers and report."""
    provider = registry.get_provider(link.connection.provider)
    tiers = (
        TierLink.objects.filter(event_link=link).select_related("tier").order_by("tier__display_order", "tier__name")
    )
    return EventLinkSchema(
        provider=provider.key,
        display_name=provider.display_name,
        remote_id=link.remote_id,
        remote_url=link.remote_url,
        remote_status=t.cast(t.Any, link.remote_status),
        sync_state=t.cast(t.Any, link.sync_state),
        origin=t.cast(t.Any, link.origin),
        auto_sync=link.auto_sync,
        effective_auto_sync=link.effective_auto_sync,
        last_pushed_at=link.last_pushed_at,
        last_pulled_at=link.last_pulled_at,
        sync_report=[SyncReportEntry.model_validate(e) for e in link.sync_report],
        tiers=[
            TierLinkSchema(
                tier_id=tl.tier.id,
                tier_name=tl.tier.name,
                remote_id=tl.remote_id,
                remote_quantity_sold=tl.remote_quantity_sold,
                counts_updated_at=tl.counts_updated_at,
                remote_paused=tl.remote_paused,
            )
            for tl in tiers
            if tl.tier is not None  # orphan links keep a deleted tier's remote_id for reconcile only
        ],
    )


def list_links(event: Event) -> list[EventLinkSchema]:
    """Every link this event has, across providers. Links of a disabled provider are skipped."""
    return [
        to_link_schema(link)
        for link in EventLink.objects.filter(event=event).select_related("connection").order_by("created_at")
        if link.connection.provider in registry.PROVIDERS
    ]


def _require_pushed_link(event: Event, provider_key: str) -> EventLink:
    link = get_link(event, provider_key)
    if link is None or not link.remote_id:
        raise IntegrationError(
            IntegrationErrorCode.PROVIDER_NOT_CONNECTED, str(_("Push the event to the platform first.")), status=404
        )
    if link.sync_state == EventLink.SyncState.BROKEN:
        raise IntegrationError(
            IntegrationErrorCode.REMOTE_EVENT_MISSING,
            str(_("The listing no longer exists on the platform. Push again to recreate it.")),
            status=409,
        )
    return link


def publish_link(event: Event, provider_key: str) -> EventLink:
    """Explicit publish (spec §7.4). Synchronous so the organizer sees the platform's answer."""
    link = _require_pushed_link(event, provider_key)
    if link.remote_status == EventLink.RemoteStatus.LIVE:
        return link
    provider = registry.get_provider(provider_key)
    try:
        provider.publish_event(link.connection.token(), link.remote_id)
    except ProviderError as e:
        if e.code == IntegrationErrorCode.REMOTE_EVENT_MISSING:
            link.sync_state, link.remote_id = EventLink.SyncState.BROKEN, ""
            TierLink.objects.filter(event_link=link).delete()
            link.save(update_fields=["sync_state", "remote_id", "updated_at"])
            raise IntegrationError(
                e.code,
                str(_("The listing no longer exists on the platform. Push again to recreate it.")),
                e.provider_message,
                status=409,
            ) from e
        if e.code == IntegrationErrorCode.CONNECTION_REVOKED:
            connection_service.mark_revoked(link.connection)
        status = (
            502
            if e.code in (IntegrationErrorCode.PROVIDER_REJECTED, IntegrationErrorCode.PROVIDER_RATE_LIMITED)
            else 400
        )
        raise IntegrationError(
            e.code, str(_("The platform refused to publish the listing.")), e.provider_message, status=status
        ) from e
    link.remote_status = EventLink.RemoteStatus.LIVE
    link.save(update_fields=["remote_status", "updated_at"])
    logger.info("integration_published", link_id=str(link.id), provider=provider_key)
    return link


def set_link_auto_sync(event: Event, provider_key: str, auto_sync: bool | None) -> EventLink:
    """Per-event override of the connection's auto-sync default (null = inherit)."""
    link = get_link(event, provider_key)
    if link is None:
        raise IntegrationError(
            IntegrationErrorCode.PROVIDER_NOT_CONNECTED, str(_("Push the event to the platform first.")), status=404
        )
    link.auto_sync = auto_sync
    link.save(update_fields=["auto_sync", "updated_at"])
    return link
