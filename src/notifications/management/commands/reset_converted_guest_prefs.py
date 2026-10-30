"""Legacy backfill: give guests converted before #1248 the regular notification defaults.

Run by an operator, dry-run first::

    python manage.py reset_converted_guest_prefs                      # dry-run, counts only
    python manage.py reset_converted_guest_prefs --apply              # write

Exact rule, deliberately conservative:

* A non-guest row is matched only when EVERY key of ORIGINAL_GUEST_KEYS is present and disabled
  (``enabled`` false, whatever the channels). If one of them is enabled the user changed their own
  settings and the row is skipped.
* For a matched row ONLY the ORIGINAL_GUEST_KEYS entries are reset (to the regular default for that
  key, or removed when there is none), and missing regular-default keys are added with ``setdefault``.
* Every other existing key is never modified or deleted: migrations 0028/0033 rewrote non-original
  keys, so their shape no longer tells guest leftovers apart from user choices. With
  ``--include-pinned-defaults``, on matched rows ``org_contact_message_received`` and
  ``potluck_item_created_and_claimed`` are also reset when their value is EXACTLY ``{"enabled": False}``.

ORIGINAL_GUEST_KEYS are the keys present in the guest map from its refactored form (6d7a1cbf,
2025-11-16) up to today, minus later additions: potluck_item_created_and_claimed (73547ce5, backfilled
ENABLED for converted users by migration 0016), org_contact_message_received (c14cda46, backfilled
ENABLED by 0021), membership_card_updated (1004e0d6, 2026-08-11) and the staff_* types (249788fc, b222fbb0).
Rows created before 6d7a1cbf (migration 0002 era: invitation_claimed, event_created, ...) are not matched.

Output is counts only (plus user ids with ``--show-ids``); never emails.

# ponytail: guest rows created before 6d7a1cbf (about 2025-11-11 to 16) are not matched; hand-repair.
# ponytail: guests converted between #1127 and the #1253 deploy may keep leftover ``staff_*`` /
# ``membership_card_updated`` ``{"enabled": False}`` entries; runtime conversions are fixed.
# ponytail: a regular user who disabled every original guest key would be matched. Vanishingly
# unlikely, and there is no guest history to discriminate.
"""

import typing as t
from collections import Counter
from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from django.utils import timezone

from notifications.models import NotificationPreference, get_default_notification_type_settings
from notifications.service.preferences import is_disabled_entry

BATCH_SIZE = 500

ORIGINAL_GUEST_KEYS: tuple[str, ...] = (
    "event_open",
    "potluck_item_created",
    "potluck_item_updated",
    "potluck_item_claimed",
    "potluck_item_unclaimed",
    "questionnaire_submitted",
    "invitation_request_created",
    "membership_request_created",
    "membership_granted",
    "membership_promoted",
    "membership_removed",
    "membership_request_approved",
    "membership_request_rejected",
    "org_announcement",
)
# Backfilled ENABLED for converted users by 0016/0021, so they are not original keys; may still hold
# the guest-creation value on rows that were never touched.
PINNED_KEYS: tuple[str, ...] = ("org_contact_message_received", "potluck_item_created_and_claimed")


def _enabled_original(settings: dict[str, t.Any]) -> bool:
    return any(isinstance(settings.get(k), dict) and settings[k].get("enabled", True) for k in ORIGINAL_GUEST_KEYS)


@dataclass
class Stats:
    scanned: int = 0
    matched: int = 0
    skipped_enabled: int = 0
    reset_per_key: Counter[str] = field(default_factory=Counter)
    pinned_candidates: int = 0
    pinned_reset: int = 0
    changed: int = 0
    ids: list[str] = field(default_factory=list)


def _plan(settings: t.Any, include_pinned: bool, stats: Stats) -> dict[str, t.Any] | None:
    """Return the new settings for a matched row, or None (skipped / not matched)."""
    if not isinstance(settings, dict):
        return None
    if not all(is_disabled_entry(settings.get(k)) for k in ORIGINAL_GUEST_KEYS):
        if _enabled_original(settings):
            stats.skipped_enabled += 1
        return None
    stats.matched += 1
    regular = {str(k): v for k, v in get_default_notification_type_settings().items()}
    updated = dict(settings)
    for key in ORIGINAL_GUEST_KEYS:
        stats.reset_per_key[key] += 1
        if key in regular:
            updated[key] = regular[key]
        else:
            del updated[key]
    for key in PINNED_KEYS:
        if updated.get(key) == {"enabled": False}:
            stats.pinned_candidates += 1
            if include_pinned and key in regular:
                updated[key] = regular[key]
                stats.pinned_reset += 1
    for key, value in regular.items():
        updated.setdefault(key, value)
    return updated


class Command(BaseCommand):
    """Dry-run by default; ``--apply`` writes."""

    help = "Reset notification preferences of guests converted before #1248 (dry-run unless --apply)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Declare ``--apply``, ``--include-pinned-defaults`` and ``--show-ids``."""
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry-run).")
        parser.add_argument(
            "--include-pinned-defaults",
            action="store_true",
            help="Also reset org_contact_message_received / potluck_item_created_and_claimed on matched rows "
            'when their value is exactly {"enabled": False}.',
        )
        parser.add_argument("--show-ids", type=int, default=0, metavar="N", help="Print up to N matched user ids.")

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Scan non-guest preference rows in pk batches and report (or write) the resets."""
        apply: bool = options["apply"]
        include_pinned: bool = options["include_pinned_defaults"]
        show_ids: int = options["show_ids"]
        stats = Stats()
        candidates = NotificationPreference.objects.filter(
            user__guest=False, notification_type_settings__has_key=ORIGINAL_GUEST_KEYS[0]
        ).order_by("pk")
        # pk-sliced batches, not .iterator(): server-side cursors break behind PgBouncer.
        last_pk = None
        while True:
            with transaction.atomic():
                batch_qs = candidates if last_pk is None else candidates.filter(pk__gt=last_pk)
                if apply:
                    batch_qs = batch_qs.select_for_update(of=("self",))
                batch = list(batch_qs[:BATCH_SIZE])
                if not batch:
                    break
                last_pk = batch[-1].pk
                changed = self._plan_batch(batch, stats, include_pinned, show_ids)
                if apply and changed:
                    NotificationPreference.objects.bulk_update(changed, ["notification_type_settings", "updated_at"])
        self._report(stats, apply, include_pinned)

    def _plan_batch(
        self, batch: list[NotificationPreference], stats: Stats, include_pinned: bool, show_ids: int
    ) -> list[NotificationPreference]:
        """Plan every row of the batch in memory; return the rows whose settings changed."""
        changed: list[NotificationPreference] = []
        now = timezone.now()
        for prefs in batch:
            stats.scanned += 1
            new = _plan(prefs.notification_type_settings, include_pinned, stats)
            if new is None:
                continue
            if len(stats.ids) < show_ids:
                stats.ids.append(str(prefs.user_id))
            if new != prefs.notification_type_settings:
                stats.changed += 1
                prefs.notification_type_settings = new
                prefs.updated_at = now
                changed.append(prefs)
        return changed

    def _report(self, stats: Stats, apply: bool, include_pinned: bool) -> None:
        w = self.stdout.write
        w(f"mode: {'APPLY' if apply else 'dry-run'}")
        w(f"rows scanned: {stats.scanned}")
        w(f"rows matched: {stats.matched}")
        w(f"rows changed{'' if apply else ' (would be)'}: {stats.changed}")
        w(f"rows skipped (an original key is enabled): {stats.skipped_enabled}")
        for key, count in sorted(stats.reset_per_key.items()):
            w(f"  {key}: {count}")
        w(f"pinned-key candidates (exactly {{'enabled': False}}): {stats.pinned_candidates}")
        if include_pinned:
            w(f"pinned keys reset{'' if apply else ' (would be)'}: {stats.pinned_reset}")
        if stats.ids:
            w("matched user ids: " + ", ".join(stats.ids))
