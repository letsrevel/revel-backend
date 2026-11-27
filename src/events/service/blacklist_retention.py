"""Keep the blacklist entries of an account in place when the account is deleted or erased."""

import typing as t

import structlog
from django.db import transaction
from django.db.models import Q

from accounts.models import RevelUser
from events.models import Blacklist, Organization
from events.service.blacklist_service import lock_organization_for_staff_changes

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
        q |= Q(telegram_username__iexact=entry.telegram_username)
    return q


def _holds(other: Blacklist, field: str, value: str) -> bool:
    """Whether ``other`` holds ``value`` in ``field``. Email and Telegram compare without case, phone exactly."""
    stored = getattr(other, field)
    if not stored:
        return False
    return bool(stored.lower() == value.lower() if field in ("email", "telegram_username") else stored == value)


def _merge_into_unlinked(entry: Blacklist, holders: list[Blacklist]) -> None:
    """Move the identifiers of ``entry`` onto unlinked entries, so every identifier stays enforced.

    An identifier that a holder already holds is dropped from ``entry``. Each other identifier moves to the
    oldest holder when that holder has no value for the field. The reason moves when the holder has none.
    If an identifier is left, ``entry`` stays as an unlinked entry with only those identifiers.
    Otherwise ``entry`` is deleted.
    """
    target = holders[0]
    updates: dict[str, str] = {}
    leftover: dict[str, str] = {}
    for field in _IDENTIFIER_FIELDS:
        value = getattr(entry, field)
        if not value or any(_holds(other, field, value) for other in holders):
            continue
        if getattr(target, field):
            leftover[field] = value
        else:
            updates[field] = value
    if entry.reason and not target.reason:
        updates["reason"] = entry.reason
    if updates:
        Blacklist.objects.filter(pk=target.pk).update(**updates)
    if leftover:
        cleared: dict[str, t.Any] = {f: None for f in _IDENTIFIER_FIELDS if f not in leftover}
        Blacklist.objects.filter(pk=entry.pk).update(
            user=None, first_name=None, last_name=None, preferred_name=None, **cleared
        )
    else:
        Blacklist.objects.filter(pk=entry.pk).delete()


def detach_blacklist_entries_of_account(user: RevelUser) -> None:
    """Turn the blacklist entries linked to ``user`` into unlinked entries, so they outlive the account.

    The email, phone number, Telegram username and reason stay. The name fields are cleared.
    If an unlinked entry of the same organization already holds one of the identifiers, the
    entry cannot be unlinked without breaking the unique constraints. Then each identifier that no
    unlinked entry holds moves to the oldest holder when the holder has no value for it. An identifier
    that an unlinked entry already holds stays on that entry. The reason moves when the holder has none.
    If an identifier cannot move, the entry stays as an unlinked entry with only that identifier.
    The entry is deleted only when no identifier is left.

    The organizations of the entries are locked first, one at a time in ascending pk order, with
    ``lock_organization_for_staff_changes``. It is the lock that ``add_to_blacklist`` takes, so a
    concurrent manual add or a second deletion in the same organization waits. The caller holds the
    account row already, so the lock order is ``RevelUser`` then ``Organization``
    (see "Lock order" in ``docs/engineering-notes.md``).

    Writes use queryset updates, so no model save logic runs.

    Args:
        user: The account that is deleted or erased.
    """
    with transaction.atomic():
        organization_ids = set(Blacklist.objects.filter(user=user).values_list("organization_id", flat=True))
        for organization in Organization.objects.filter(pk__in=organization_ids).order_by("pk"):
            lock_organization_for_staff_changes(organization)
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
            log_fields = {
                "user_id": str(user.pk),
                "entry_ids": [str(e.pk) for e in entries],
                "organization_ids": [str(e.organization_id) for e in entries],
                "count": len(entries),
                "merged": merged,
            }
            transaction.on_commit(lambda: logger.info("blacklist_entries_detached", **log_fields))
