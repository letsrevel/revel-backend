"""Keep the blacklist entries of an account in place when the account is deleted or erased."""

import typing as t

import structlog
from django.db import IntegrityError, transaction
from django.db.models import Q

from accounts.models import RevelUser
from accounts.utils.email_normalization import normalize_telegram_for_matching
from events.models import Blacklist, Organization
from events.service.blacklist_service import lock_organization_for_staff_changes
from events.service.holder_visibility import is_known_holder_id
from telegram.models import TelegramUser

logger = structlog.get_logger(__name__)

_Field = t.Literal["email", "phone_number", "telegram_username"]
_IDENTIFIER_FIELDS: tuple[_Field, ...] = ("email", "phone_number", "telegram_username")


class _Identifiers(t.TypedDict):
    """Identifier values by field."""

    email: list[str]
    phone_number: list[str]
    telegram_username: list[str]


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
    oldest holder that has no value for the field. The reason moves to the oldest holder and to every holder that
    receives an identifier, when it has none.
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
    if entry.reason:
        # The oldest holder and every holder that receives an identifier get the reason when they have none.
        for holder in {holders[0], *updates}:
            if not holder.reason:
                updates.setdefault(holder, {})["reason"] = entry.reason
    # A Django admin save takes the same organization lock (BlacklistAdmin.save_model), so no new admin entry can
    # appear between the holder read and these updates. An admin edit of an existing entry is last-write-wins (#1616).
    for holder, values in updates.items():
        Blacklist.objects.filter(pk=holder.pk).update(**values)
    if leftover:
        cleared: dict[str, t.Any] = {f: None for f in _IDENTIFIER_FIELDS if f not in leftover}
        Blacklist.objects.filter(pk=entry.pk).update(
            user=None, first_name=None, last_name=None, preferred_name=None, **cleared, **leftover
        )
        return False
    Blacklist.objects.filter(pk=entry.pk).delete()
    return True


def _lock_entries_and_organizations(user: RevelUser) -> list[Blacklist]:
    """Lock the organizations of the linked entries (pk order), then the entries, until the set is stable."""
    # ponytail: a bulk delete of several accounts, or a later pass that finds a new organization, can take locks
    # out of the global order. Postgres then aborts one transaction, and the whole deletion rolls back: nothing is
    # written and no entry is lost. delete_user_account does not retry it. A self-service deletion comes back
    # active and must be requested again; an admin, ban or sweep run stays inactive with the existing
    # `account_deletion_failed` error log (no alert) until an operator queues it again. Every production path
    # deletes one account per task. Upgrade path: lock all accounts and organizations of a bulk delete up front.
    locked: set[t.Any] = set()
    while True:
        new_ids = set(Blacklist.objects.filter(user=user).values_list("organization_id", flat=True)) - locked
        for organization in Organization.objects.filter(pk__in=new_ids).order_by("pk"):
            lock_organization_for_staff_changes(organization)
        locked |= new_ids
        entries = list(Blacklist.objects.select_for_update().filter(user=user).order_by("created_at", "pk"))
        if {e.organization_id for e in entries} <= locked:
            return entries


def _current_identifiers(user: RevelUser) -> _Identifiers:
    """The identifiers of the account now: email (lowercase), phone number and Telegram usernames (normalized)."""
    usernames = [
        normalize_telegram_for_matching(name)
        for name in TelegramUser.objects.filter(user=user)
        .order_by("created_at", "pk")
        .values_list("telegram_username", flat=True)
        if name
    ]
    return _Identifiers(
        email=[user.email.strip().lower()] if user.email else [],
        phone_number=[user.phone_number] if user.phone_number else [],
        telegram_username=[u for u in dict.fromkeys(usernames) if u],
    )


def _fill_and_find_extras(entry: Blacklist, current: _Identifiers) -> _Identifiers:
    """Fill each empty identifier of ``entry`` from the account. Return the current values that differ from it."""
    extras = _Identifiers(email=[], phone_number=[], telegram_username=[])
    for field in _IDENTIFIER_FIELDS:
        values = list(current[field])
        if not getattr(entry, field) and values:
            setattr(entry, field, values.pop(0))
        stored = getattr(entry, field)
        extras[field] = [v for v in values if not (stored and _holds(entry, field, v))]
    return extras


def _identifiers_for(entry: Blacklist, user: RevelUser, current: _Identifiers) -> _Identifiers:
    """The account identifiers that may go on ``entry``: the email always, phone and Telegram for a known holder."""
    if is_known_holder_id(entry.organization_id, user.pk):
        return current
    return _Identifiers(email=current["email"], phone_number=[], telegram_username=[])


def _free_values(entry: Blacklist, extras: _Identifiers) -> dict[str, str]:
    """The first extra value of each field that no unlinked entry of the organization holds."""
    new_values: dict[str, str] = {}
    for field in _IDENTIFIER_FIELDS:
        free = [v for v in extras[field] if not _holder_query_for(field, v, entry.organization_id).exists()]
        if free:
            new_values[field] = free[0]
        # ponytail: one value per field. More Telegram handles on the account are not kept.
        # Upgrade path: one new entry per extra handle.
    return new_values


def _keep_extras(entry: Blacklist, extras: _Identifiers) -> bool:
    """Put the extra identifiers that no unlinked entry of the organization holds on one new unlinked entry.

    The new entry has no creator: the creator may be an account that the same bulk delete removes.
    A manual add, the Django admin save and this code take the organization lock. So no other entry should
    take a value between the check and the insert. The insert still runs in a savepoint. When it loses a race,
    the free values are checked again once. When the second attempt fails too, it raises, and the whole
    deletion rolls back.

    Returns:
        Whether a new entry was created.

    Raises:
        IntegrityError: When both inserts lose a race. The whole deletion then rolls back.
    """
    for _attempt in range(2):
        new_values = _free_values(entry, extras)
        if not new_values:
            return False
        try:
            with transaction.atomic():
                Blacklist.objects.bulk_create(
                    [Blacklist(organization_id=entry.organization_id, reason=entry.reason, **new_values)]
                )
            return True
        except IntegrityError:
            # The value that clashed is held by the concurrent entry, so its ban stays; retry with the rest.
            continue
    # Both attempts lost a race: fail the deletion (it rolls back) rather than drop an identifier.
    raise IntegrityError("A blacklist identifier of the deleted account could not be kept.")


def _holder_query_for(field: str, value: str, organization_id: t.Any) -> t.Any:
    """The unlinked entries of the organization that hold ``value`` in ``field`` (email and Telegram without case)."""
    lookup = {field: value} if field == "phone_number" else {f"{field}__iexact": value}
    return Blacklist.objects.filter(organization_id=organization_id, user__isnull=True, **lookup)


def detach_blacklist_entries_of_account(user: RevelUser) -> None:
    """Turn the blacklist entries linked to ``user`` into unlinked entries, so they outlive the account.

    The email, phone number, Telegram username and reason stay. The name fields are cleared.
    The entry also keeps the email, phone number and Telegram username that the account has now. An empty
    field of the entry is filled from the account. The phone number and Telegram username of the account are
    used only when the account is a known holder of the organization of the entry; the email always is.
    A current value that differs from the stored value of the
    entry goes on one new unlinked entry of the same organization (same reason, no creator, no names),
    unless an unlinked entry of the organization already holds it.
    If an unlinked entry of the same organization already holds one of the identifiers, the
    entry cannot be unlinked without breaking the unique constraints. Then each identifier that no
    unlinked entry holds moves to the oldest holder that has no value for it. An identifier
    that an unlinked entry already holds stays on that entry. The reason moves to the oldest holder and to
    each holder that receives an identifier, when it has none.
    If an identifier cannot move, the entry stays as an unlinked entry with only that identifier.
    The entry is deleted only when no identifier is left. An entry with no identifier at all, also after
    the fill from the account, is deleted.

    The caller holds the account row, so the lock order is ``RevelUser`` then ``Organization``
    (see "Lock order" in ``docs/engineering-notes.md``). The organizations of the entries are locked one at a
    time in ascending pk order with ``lock_organization_for_staff_changes``. It is the lock that
    ``add_to_blacklist`` takes, so a concurrent manual add or a second deletion in the same organization waits.
    The entries are read under lock after the organizations. If an entry then belongs to an organization that
    is not locked yet, that organization is locked and the entries are read again, until the set is stable.
    Only then the holders are read.

    Writes use queryset updates and ``bulk_create``, so no model save logic runs.

    Args:
        user: The account that is deleted or erased. Call this before the account is anonymised.

    Raises:
        IntegrityError: When an identifier of the account cannot be kept because a concurrent entry won
            both inserts. The caller's transaction then rolls back the whole deletion.
    """
    with transaction.atomic():
        entries = _lock_entries_and_organizations(user)
        current = _current_identifiers(user)
        kept = merged_away = dropped = extra_created = 0
        for entry in entries:
            extras = _fill_and_find_extras(entry, _identifiers_for(entry, user, current))
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
                merged_away += deleted
            elif not q:
                Blacklist.objects.filter(pk=entry.pk).delete()
                deleted = True
                dropped += 1
            else:
                Blacklist.objects.filter(pk=entry.pk).update(
                    user=None,
                    first_name=None,
                    last_name=None,
                    preferred_name=None,
                    email=entry.email,
                    phone_number=entry.phone_number,
                    telegram_username=entry.telegram_username,
                )
                deleted = False
            if not deleted:
                kept += 1
            if _keep_extras(entry, extras):
                extra_created += 1
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
                    dropped=dropped,
                    extra_created=extra_created,
                )
            )
