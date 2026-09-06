"""Beat-driven housekeeping (spec §7.7a, §7.8, §8): count reconcile within the rate budget, delivery pruning."""

from dataclasses import dataclass
from datetime import timedelta

import structlog
from django.conf import settings
from django.db.models import F, Min, QuerySet
from django.utils import timezone

from integrations import registry
from integrations.exceptions import RetryableProviderError
from integrations.models import EventLink, PlatformConnection, WebhookDelivery
from integrations.service import sync_service

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReconcileSummary:
    """Outcome tallies for one ``reconcile_counts`` sweep."""

    refreshed: int
    skipped_for_budget: int
    failed: int


def links_due_for_reconcile() -> QuerySet[EventLink]:
    """Live listings of upcoming events on active connections, stalest counts first."""
    # Min(...) over the to-many join is a GROUP BY, not a DISTINCT — never use .distinct() here (#880 planner trap).
    return (
        EventLink.objects.select_related("connection", "event")
        .filter(
            remote_status=EventLink.RemoteStatus.LIVE,
            connection__status=PlatformConnection.Status.ACTIVE,
            event__end__gt=timezone.now(),
        )
        .exclude(sync_state=EventLink.SyncState.BROKEN)
        .filter(tier_links__isnull=False)
        .annotate(stalest=Min("tier_links__counts_updated_at"))
        .order_by(F("stalest").asc(nulls_first=True), "id")
    )


def reconcile_counts() -> ReconcileSummary:
    """Refresh counts for due links, per provider, until the shared budget reaches the reserve."""
    refreshed = skipped = failed = 0
    stopped: set[str] = set()
    unknown_budget_logged: set[str] = set()
    for link in links_due_for_reconcile():
        key = link.connection.provider
        if key in stopped or key not in registry.PROVIDERS:
            skipped += 1
            continue
        provider = registry.get_provider(key)
        remaining = provider.remaining_budget()
        if remaining is None and key not in unknown_budget_logged:
            logger.info("integration_reconcile_budget_unknown", provider=key)
            unknown_budget_logged.add(key)
        if remaining is not None and remaining < settings.INTEGRATIONS_RATE_RESERVE:
            logger.warning("integration_reconcile_budget_low", provider=key, remaining=remaining)
            stopped.add(key)
            skipped += 1
            continue
        try:
            if sync_service.refresh_counts(link):
                refreshed += 1
            else:
                failed += 1
        except RetryableProviderError as e:
            logger.warning("integration_reconcile_transient", provider=key, link_id=str(link.id), code=e.code.value)
            stopped.add(key)
            failed += 1
        except Exception:
            logger.exception("integration_reconcile_link_failed", link_id=str(link.id))
            failed += 1
    logger.info("integration_reconcile_done", refreshed=refreshed, skipped=skipped, failed=failed)
    return ReconcileSummary(refreshed=refreshed, skipped_for_budget=skipped, failed=failed)


def prune_webhook_deliveries() -> int:
    """Delete audit rows past the retention window."""
    cutoff = timezone.now() - timedelta(days=settings.INTEGRATIONS_WEBHOOK_DELIVERY_RETENTION_DAYS)
    deleted, _ = WebhookDelivery.objects.filter(created_at__lt=cutoff).delete()
    logger.info("integration_webhook_deliveries_pruned", deleted=deleted)
    return deleted
