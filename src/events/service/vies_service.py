"""Organization-specific VAT ID management.

Thin wrappers around the generic billing operations in ``common.service.vies_service``.
"""

import typing as t

from common.service.vies_service import VIESValidationResult, validate_and_update_vat_entity
from common.service.vies_service import clear_vat_fields as _clear_vat_fields
from common.service.vies_service import set_vat_id as _set_vat_id
from common.service.vies_service import update_billing_info as _update_billing_info

if t.TYPE_CHECKING:
    from events.models.organization import Organization


def validate_and_update_organization(org: "Organization") -> VIESValidationResult:
    """Validate an organization's VAT ID via VIES and update model fields."""
    return validate_and_update_vat_entity(org, entity_id=str(org.id), entity_type="org")


def set_org_vat_id(org: "Organization", vat_id: str) -> None:
    """Set the org's VAT ID and validate via VIES. Queues retry on VIES failure."""

    def _queue_retry() -> None:
        from events.tasks import revalidate_single_vat_id_task

        revalidate_single_vat_id_task.delay(str(org.id))

    _set_vat_id(
        org,
        vat_id,
        entity_id=str(org.id),
        entity_type="org",
        on_vies_unavailable=_queue_retry,
        rollback_on_invalid=False,
    )


def clear_org_vat_fields(org: "Organization") -> None:
    """Clear all VAT-related fields on an organization."""
    _clear_vat_fields(org)


def update_org_billing_info(org: "Organization", data: dict[str, t.Any]) -> None:
    """Update billing info fields on an organization."""
    _update_billing_info(org, data)
