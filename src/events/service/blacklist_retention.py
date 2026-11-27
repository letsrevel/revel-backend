"""Keep the blacklist entries of an account in place when the account is deleted or erased."""

import structlog
from django.db import transaction
from django.db.models import Q

from accounts.models import RevelUser
from events.models import Blacklist

logger = structlog.get_logger(__name__)

_IDENTIFIER_FIELDS = ("email", "phone_number", "telegram_username")


def _holder_query(entry: Blacklist) -> Q:
    """Build a query for the unlinked entries that hold one of the identifiers of ``entry``."""
    q = Q()
    if entry.email:
        q |= Q(email__iexact=entry.email)
    if entry.phone_number:
        q |= Q(phone_number=entry.phone_number)
    if entry.telegram_username:
        q |= Q(telegram_username=entry.telegram_username)
    return q


def _holds(other: Blacklist, field: str, value: str) -> bool:
    stored = getattr(other, field)
    return bool(stored) and (stored.lower() == value.lower() if field == "email" else stored == value)


def _merge_into_unlinked(entry: Blacklist, holders: list[Blacklist]) -> None:
    """Merge ``entry`` into the oldest holder and delete it, so the unlinked-only constraints hold."""
    target = holders[0]
    updates: dict[str, str] = {}
    for field in _IDENTIFIER_FIELDS:
        value = getattr(entry, field)
        if not value or getattr(target, field):
            continue
        if any(_holds(other, field, value) for other in holders[1:]):
            continue
        updates[field] = value
    if entry.reason and not target.reason:
        updates["reason"] = entry.reason
    if updates:
        Blacklist.objects.filter(pk=target.pk).update(**updates)
    Blacklist.objects.filter(pk=entry.pk).delete()


def detach_blacklist_entries_of_account(user: RevelUser) -> None:
    """Turn the blacklist entries linked to ``user`` into unlinked entries, so they outlive the account.

    The email, phone number, Telegram username and reason stay. The name fields are cleared.
    If an unlinked entry of the same organization already holds one of the identifiers, the
    entry cannot be unlinked without breaking the unique constraints. Then its identifiers are
    merged into the oldest such entry, and the linked entry is deleted. An identifier moves to
    the target only if the target has no value for it and no other unlinked entry holds it.
    The reason moves only when the target has none.

    Writes use queryset updates, so no model save logic runs.

    Args:
        user: The account that is deleted or erased.
    """
    with transaction.atomic():
        entries = list(Blacklist.objects.select_for_update().filter(user=user).order_by("created_at", "pk"))
        merged = 0
        for entry in entries:
            q = _holder_query(entry)
            holders = (
                list(
                    Blacklist.objects.select_for_update()
                    .filter(organization_id=entry.organization_id, user__isnull=True)
                    .filter(q)
                    .order_by("created_at", "pk")
                )
                if q
                else []
            )
            if holders:
                _merge_into_unlinked(entry, holders)
                merged += 1
            else:
                Blacklist.objects.filter(pk=entry.pk).update(
                    user=None, first_name=None, last_name=None, preferred_name=None
                )
    if entries:
        logger.info(
            "blacklist_entries_detached",
            user_id=str(user.pk),
            entry_ids=[str(e.pk) for e in entries],
            organization_ids=[str(e.organization_id) for e in entries],
            count=len(entries),
            merged=merged,
        )
