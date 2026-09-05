"""Auto-sync: Revel edits schedule a debounced full-state push for opted-in links (spec §7.5)."""

import typing as t
from functools import partial
from uuid import UUID

import structlog
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from events.models import Event, TicketTier
from integrations import registry
from integrations.models import EventLink, PlatformConnection
from integrations.service import mapper
from integrations.service.mapper import EventNotEligible

logger = structlog.get_logger(__name__)

DEBOUNCE_SECONDS = 30
# Slightly shorter than the countdown so the key expires before the queued push runs: a save
# landing just after that push must schedule the next one, not be swallowed by a stale key.
DEBOUNCE_TTL_SECONDS = 25


def debounce_key(link_id: UUID) -> str:
    """Cache key that collapses a burst of saves into one push."""
    return f"integrations:push:{link_id}"


def schedule_auto_push(event_id: UUID) -> int:
    """Schedule a debounced push for every opted-in, pushed, healthy link of the event. Returns how many."""
    from integrations.tasks import push_event_link

    def _apply_push_async(link_id: str) -> None:
        push_event_link.apply_async(args=(link_id,), countdown=DEBOUNCE_SECONDS)

    links = (
        EventLink.objects.select_related("connection", "event")
        .filter(event_id=event_id, connection__status=PlatformConnection.Status.ACTIVE)
        .exclude(remote_id="")
        .exclude(sync_state=EventLink.SyncState.BROKEN)
    )
    scheduled = 0
    for link in links:
        if not link.effective_auto_sync or link.connection.provider not in registry.PROVIDERS:
            continue
        try:
            mapper.check_eligible(link.event)
        except EventNotEligible:
            continue
        try:
            added = cache.add(debounce_key(link.id), 1, DEBOUNCE_TTL_SECONDS)
        except Exception as e:
            # Fail open: the cache backend raises on connection failures (see the CACHES
            # comment in revel/settings/base.py). A save must never break because of it —
            # skip scheduling this time; the organizer can push manually and the next save retries.
            logger.warning("integration_auto_sync_cache_unavailable", link_id=str(link.id), error=str(e))
            continue
        if not added:
            continue
        EventLink.objects.filter(pk=link.pk).update(sync_state=EventLink.SyncState.PENDING)
        transaction.on_commit(partial(_apply_push_async, str(link.id)))
        scheduled += 1
    if scheduled:
        logger.info("integration_auto_sync_scheduled", event_id=str(event_id), links=scheduled)
    return scheduled


@receiver(post_save, sender=Event, dispatch_uid="integrations_event_saved")
def _on_event_saved(sender: type[Event], instance: Event, raw: bool = False, **kwargs: t.Any) -> None:
    if raw or instance.is_template:
        return
    schedule_auto_push(instance.id)


@receiver(post_save, sender=TicketTier, dispatch_uid="integrations_tier_saved")
def _on_tier_saved(sender: type[TicketTier], instance: TicketTier, raw: bool = False, **kwargs: t.Any) -> None:
    if raw:
        return
    schedule_auto_push(instance.event_id)


@receiver(post_delete, sender=TicketTier, dispatch_uid="integrations_tier_deleted")
def _on_tier_deleted(sender: type[TicketTier], instance: TicketTier, **kwargs: t.Any) -> None:
    schedule_auto_push(instance.event_id)
