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


def _merge_into_unlinked(entry: Blacklist, holders: list[Blacklist]) -> bool:
    """Move the identifiers of ``entry`` onto unlinked entries, so every identifier stays enforced.

    An identifier that a holder already holds is dropped from ``entry``. Each other identifier moves to the
    oldest holder that has no value for the field. The reason moves to the oldest holder when it has none.
    If no holder can take an identifier, it stays on ``entry``, which stays as an unlinked entry with only
    those identifiers. Otherwise ``entry`` is deleted.

    Returns:
        Whether ``entry`` was deleted.
    """
    updates: dict[Blacklist, dict[str, str]] = {}
    leftover: dict[str, str] = {}
    for field in _IDENTIFIER_FIELDS:
        value = getattr(entry, field)
        if not value or any(_holds(other, field, value) for other in holders):
            continue
        free = next((other for other in holders if not getattr(other, field)), None)
        if free is None:
            leftover[field] = value
        else:
            updates.setdefault(free, {})[field] = value
    if entry.reason and not holders[0].reason:
        updates.setdefault(holders[0], {})["reason"] = entry.reason
    for holder, values in updates.items():
        Blacklist.objects.filter(pk=holder.pk).update(**values)
    if leftover:
        cleared: dict[str, t.Any] = {f: None for f in _IDENTIFIER_FIELDS if f not in leftover}
        Blacklist.objects.filter(pk=entry.pk).update(
            user=None, first_name=None, last_name=None, preferred_name=None, **cleared
        )
        return False
    Blacklist.objects.filter(pk=entry.pk).delete()
    return True


def _lock_entries_and_organizations(user: RevelUser) -> list[Blacklist]:
    """Lock the organizations of the linked entries (pk order), then the entries, until the set is stable."""
    locked: set[t.Any] = set()
    while True:
        new_ids = set(Blacklist.objects.filter(user=user).values_list("organization_id", flat=True)) - locked
        for organization in Organization.objects.filter(pk__in=new_ids).order_by("pk"):
            lock_organization_for_staff_changes(organization)
        locked |= new_ids
        entries = list(Blacklist.objects.select_for_update().filter(user=user).order_by("created_at", "pk"))
        if {e.organization_id for e in entries} <= locked:
            return entries


def detach_blacklist_entries_of_account(user: RevelUser) -> None:
    """Turn the blacklist entries linked to ``user`` into unlinked entries, so they outlive the account.

    The email, phone number, Telegram username and reason stay. The name fields are cleared.
    If an unlinked entry of the same organization already holds one of the identifiers, the
    entry cannot be unlinked without breaking the unique constraints. Then each identifier that no
    unlinked entry holds moves to the oldest holder that has no value for it. An identifier
    that an unlinked entry already holds stays on that entry. The reason moves when the oldest holder has none.
    If an identifier cannot move, the entry stays as an unlinked entry with only that identifier.
    The entry is deleted only when no identifier is left. An entry with no identifier at all is deleted.

    The caller holds the account row, so the lock order is ``RevelUser`` then ``Organization``
    (see "Lock order" in ``docs/engineering-notes.md``). The organizations of the entries are locked one at a
    time in ascending pk order with ``lock_organization_for_staff_changes``. It is the lock that
    ``add_to_blacklist`` takes, so a concurrent manual add or a second deletion in the same organization waits.
    The entries are read under lock after the organizations. If an entry then belongs to an organization that
    is not locked yet, that organization is locked and the entries are read again, until the set is stable.
    Only then the holders are read.

    Writes use queryset updates, so no model save logic runs.

    Args:
        user: The account that is deleted or erased.
    """
    with transaction.atomic():
        entries = _lock_entries_and_organizations(user)
        kept = 0
        merged_away = 0
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
                deleted = _merge_into_unlinked(entry, holders)
            elif not q:
                Blacklist.objects.filter(pk=entry.pk).delete()
                deleted = True
            else:
                Blacklist.objects.filter(pk=entry.pk).update(
                    user=None, first_name=None, last_name=None, preferred_name=None
                )
                deleted = False
            if deleted:
                merged_away += 1
            else:
                kept += 1
        if entries:
            user_id = str(user.pk)
            entry_ids = [str(e.pk) for e in entries]
            organization_ids = [str(e.organization_id) for e in entries]
            count = len(entries)
            transaction.on_commit(
                lambda: logger.info(
                    "blacklist_entries_detached",
                    user_id=user_id,
                    entry_ids=entry_ids,
                    organization_ids=organization_ids,
                    count=count,
                    kept=kept,
                    merged_away=merged_away,
                )
            )
