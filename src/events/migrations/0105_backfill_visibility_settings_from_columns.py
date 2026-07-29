"""Fold ``address_visibility`` and ``public_pronoun_distribution`` into the JSON blob (#793).

The columns are dropped by 0106. This migration must therefore be exactly
reversible: reverting the release without reverting the migration would leave
old code SELECTing dropped columns, so a rollback runs ``migrate events 0104``,
and that path has to restore both columns from the blob.

Reversing also **strips both keys** from the blob. ``EventVisibilitySettings``
is ``extra="forbid"``, so a rollback that left them behind would make
``validate_visibility_settings`` raise for every migrated event — that is,
every event in the database.
"""

import typing as t

from django.db import migrations

if t.TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor

ADDRESS_KEY = "address_visibility"
PRONOUN_KEY = "show_pronoun_distribution"

# Frozen copies of the column defaults. Deliberately literals rather than
# imports from ``events.utils.visibility_settings``: a migration must keep
# behaving the same after the application model moves on.
DEFAULT_ADDRESS_VISIBILITY = "public"
DEFAULT_SHOW_PRONOUN_DISTRIBUTION = False

BATCH_SIZE = 500


def merge_into_blob(
    blob: dict[str, t.Any] | None,
    address_visibility: str,
    public_pronoun_distribution: bool,
) -> dict[str, t.Any]:
    """Fold the two column values onto an event's existing visibility blob.

    Merges rather than replaces: the ``show_capacity`` / ``show_attendee_count``
    / ``show_attendee_list`` toggles written by #792 must survive untouched.
    Returns a new dict — callers batch these objects, and mutating in place
    would alias rows together.

    Args:
        blob: The event's current ``visibility_settings`` (possibly empty/None).
        address_visibility: The ``address_visibility`` column value.
        public_pronoun_distribution: The ``public_pronoun_distribution`` column value.

    Returns:
        The merged blob, carrying both new keys.
    """
    return {
        **(blob or {}),
        ADDRESS_KEY: address_visibility,
        PRONOUN_KEY: public_pronoun_distribution,
    }


def split_out_of_blob(blob: dict[str, t.Any] | None) -> tuple[str, bool, dict[str, t.Any]]:
    """Recover the two column values from a blob and strip their keys.

    The strip is mandatory, not cosmetic — see the module docstring.

    Args:
        blob: The event's ``visibility_settings`` (possibly empty/None).

    Returns:
        ``(address_visibility, public_pronoun_distribution, remaining_blob)``.
        Missing keys fall back to the original column defaults, which covers
        rows created between the code deploy and this migration.
    """
    blob = blob or {}
    return (
        blob.get(ADDRESS_KEY, DEFAULT_ADDRESS_VISIBILITY),
        bool(blob.get(PRONOUN_KEY, DEFAULT_SHOW_PRONOUN_DISTRIBUTION)),
        {key: value for key, value in blob.items() if key not in (ADDRESS_KEY, PRONOUN_KEY)},
    )


def _id_batches(model: t.Any) -> t.Iterator[list[t.Any]]:
    """Yield primary keys in fixed-size batches.

    Materializes the id list up front instead of using ``.iterator()``: server-side
    cursors are disabled under PgBouncer (``DISABLE_SERVER_SIDE_CURSORS``), and a
    list of UUIDs is cheap even for a large events table.
    """
    ids = list(model.objects.order_by("pk").values_list("pk", flat=True))
    for start in range(0, len(ids), BATCH_SIZE):
        yield ids[start : start + BATCH_SIZE]


def forwards(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    """Copy both columns into ``visibility_settings`` for every event."""
    Event = apps.get_model("events", "Event")
    for batch in _id_batches(Event):
        events = list(Event.objects.filter(pk__in=batch))
        for event in events:
            event.visibility_settings = merge_into_blob(
                event.visibility_settings,
                event.address_visibility,
                event.public_pronoun_distribution,
            )
        Event.objects.bulk_update(events, ["visibility_settings"])


def backwards(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    """Restore both columns from ``visibility_settings`` and strip the keys."""
    Event = apps.get_model("events", "Event")
    for batch in _id_batches(Event):
        events = list(Event.objects.filter(pk__in=batch))
        for event in events:
            address_visibility, show_pronouns, remainder = split_out_of_blob(event.visibility_settings)
            event.address_visibility = address_visibility
            event.public_pronoun_distribution = show_pronouns
            event.visibility_settings = remainder
        Event.objects.bulk_update(events, ["address_visibility", "public_pronoun_distribution", "visibility_settings"])


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0104_event_visibility_settings"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
