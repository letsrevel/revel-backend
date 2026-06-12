"""Celery tasks for VAT-ID (VIES) re-validation."""

import typing as t

import structlog
from celery import shared_task

from events.models import Organization

logger = structlog.get_logger(__name__)


class VatRevalidationResult(t.TypedDict):
    """Telemetry counters returned by ``revalidate_vat_ids_task``."""

    dispatched: int


@shared_task(name="events.revalidate_vat_ids")
def revalidate_vat_ids_task() -> VatRevalidationResult:
    """Dispatch per-org VIES re-validation tasks.

    Runs on the 15th of each month via Celery Beat.
    Each org gets its own task with independent retry.
    """
    org_ids = list(Organization.objects.filter(vat_id__gt="").values_list("id", flat=True))

    for org_id in org_ids:
        revalidate_single_vat_id_task.delay(str(org_id))

    logger.info("vat_revalidation_dispatched", org_count=len(org_ids))
    return {"dispatched": len(org_ids)}


@shared_task(
    name="events.revalidate_single_vat_id",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=3600,
    max_retries=5,
)
def revalidate_single_vat_id_task(org_id: str) -> None:
    """Re-validate a single organization's VAT ID via VIES.

    Retries with exponential backoff on VIES unavailability or network errors.
    Fails loudly on unexpected errors after max retries.
    """
    from events.service.vies_service import validate_and_update_organization

    org = Organization.objects.get(pk=org_id)
    if not org.vat_id:
        return

    validate_and_update_organization(org)
    logger.info("vat_revalidation_done", org_id=org_id, vat_id=org.vat_id, valid=org.vat_id_validated)
