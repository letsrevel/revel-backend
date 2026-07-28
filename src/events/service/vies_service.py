"""Organization-specific VAT ID management.

Thin wrappers around the generic billing operations in ``common.service.vies_service``.

Each wrapper also compares the org's *effective* subscription fee percent
(:func:`events.service.subscription_stripe_service.effective_application_fee_percent`)
before and after the mutation: a VAT-status change (validation flip, VAT ID
set/cleared, country change) changes the VAT gross-up baked into Stripe's
``application_fee_percent`` at Checkout, so live subscriptions must be resynced
or the collected fee drifts from the ledger's decomposition.
"""

import typing as t
from decimal import Decimal

from django.db import transaction

from common.service.vies_service import VIESValidationResult, validate_and_update_vat_entity
from common.service.vies_service import clear_vat_fields as _clear_vat_fields
from common.service.vies_service import set_vat_id as _set_vat_id
from common.service.vies_service import update_billing_info as _update_billing_info

if t.TYPE_CHECKING:
    from events.models.organization import Organization


def _effective_fee_percent(org: "Organization") -> Decimal | None:
    """Current effective subscription fee percent (lazy import: avoid cycles)."""
    from events.service.subscription_stripe_service import effective_application_fee_percent  # noqa: PLC0415

    return effective_application_fee_percent(org)


def _dispatch_fee_resync_if_changed(org: "Organization", before: Decimal | None) -> None:
    """Queue a subscription-fee resync when the effective percent changed.

    Runs in a ``finally`` in the wrappers below: several generic operations
    persist a VAT-state change *and then* raise (invalid VAT ID saves
    ``vat_id_validated=False`` before the 400; a VIES outage saves the ID
    unvalidated before the 503 — and Ninja turns the ``HttpError`` into a
    response, so under ``ATOMIC_REQUESTS`` the transaction still commits).
    Deferred via ``on_commit`` so the task never races the commit; outside a
    transaction (Celery) it runs immediately.
    """
    if _effective_fee_percent(org) == before:
        return

    def _queue() -> None:
        from events.tasks import resync_org_subscription_fees  # noqa: PLC0415

        resync_org_subscription_fees.delay(str(org.id))

    transaction.on_commit(_queue)


def validate_and_update_organization(org: "Organization") -> VIESValidationResult:
    """Validate an organization's VAT ID via VIES and update model fields."""
    before = _effective_fee_percent(org)
    try:
        return validate_and_update_vat_entity(org, entity_id=str(org.id), entity_type="org")
    finally:
        _dispatch_fee_resync_if_changed(org, before)


def set_org_vat_id(org: "Organization", vat_id: str) -> None:
    """Set the org's VAT ID and validate via VIES. Queues retry on VIES failure."""

    def _queue_retry() -> None:
        from events.tasks import revalidate_single_vat_id_task

        # _queue_retry runs right before set_vat_id raises HttpError(503). There
        # is no inner atomic block, and Django Ninja turns the HttpError into a
        # 503 *response* (no exception reaches the ATOMIC_REQUESTS wrapper), so
        # the request transaction commits and on_commit fires — deferring avoids
        # the retry task racing the commit of the org's freshly-set vat_id.
        transaction.on_commit(lambda: revalidate_single_vat_id_task.delay(str(org.id)))

    before = _effective_fee_percent(org)
    try:
        _set_vat_id(
            org,
            vat_id,
            entity_id=str(org.id),
            entity_type="org",
            on_vies_unavailable=_queue_retry,
            rollback_on_invalid=False,
        )
    finally:
        _dispatch_fee_resync_if_changed(org, before)


def clear_org_vat_fields(org: "Organization") -> None:
    """Clear all VAT-related fields on an organization."""
    before = _effective_fee_percent(org)
    _clear_vat_fields(org)
    _dispatch_fee_resync_if_changed(org, before)


def update_org_billing_info(org: "Organization", data: dict[str, t.Any]) -> None:
    """Update billing info fields on an organization."""
    before = _effective_fee_percent(org)
    try:
        _update_billing_info(org, data)
    finally:
        _dispatch_fee_resync_if_changed(org, before)
