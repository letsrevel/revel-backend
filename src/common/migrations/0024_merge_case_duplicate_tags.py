"""Merge tags that differ only by letter case (#1517).

Per ``Lower(name)`` group, the canonical tag is the one with the most assignments
(ties: the oldest, then the lowest pk). Duplicates are merged into it.
"""

import typing as t
from collections import defaultdict

from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower

FILL_FIELDS = ("description", "color", "icon")


def _parent_of(Tag: t.Any, pk: t.Any) -> t.Any:
    """Read the live parent: earlier groups already repointed parents in the database."""
    return Tag.objects.filter(pk=pk).values_list("parent_id", flat=True).first()


def _fill_empty_fields(Tag: t.Any, canonical: t.Any, duplicates: list[t.Any]) -> None:
    """Fill the canonical's empty fields from the duplicates, most used first."""
    fills = {}
    for field in FILL_FIELDS:
        if not getattr(canonical, field):
            value = next((v for dup in duplicates if (v := getattr(dup, field))), None)
            if value:
                fills[field] = value
    if fills:
        Tag.objects.filter(pk=canonical.pk).update(**fills)


def _repoint_children(Tag: t.Any, canonical: t.Any, members: list[t.Any]) -> None:
    """Move the children of the duplicates to the canonical, without self-parenting or cycles."""
    duplicate_ids = [m.pk for m in members if m.pk != canonical.pk]
    # Never make the canonical its own parent.
    parent = _parent_of(Tag, canonical.pk)
    if parent in duplicate_ids:
        grandparent = _parent_of(Tag, parent)
        Tag.objects.filter(pk=canonical.pk).update(parent_id=None if grandparent in [m.pk for m in members] else grandparent)

    Tag.objects.filter(parent_id__in=duplicate_ids).update(parent_id=canonical.pk)

    # Repointing can close a cycle through the canonical: break it at the link back to the canonical.
    visited: set[t.Any] = set()
    previous, current = canonical.pk, _parent_of(Tag, canonical.pk)
    while current is not None and current not in visited:
        if current == canonical.pk:
            Tag.objects.filter(pk=previous).update(parent_id=None)
            break
        visited.add(current)
        previous, current = current, _parent_of(Tag, current)


def _move_assignments(TagAssignment: t.Any, canonical: t.Any, duplicate_ids: list[t.Any]) -> None:
    """Move the duplicates' assignments to the canonical; drop those the canonical already has."""
    taken = set(
        TagAssignment.objects.filter(tag=canonical).values_list("content_type_id", "object_id"),
    )
    for assignment in TagAssignment.objects.filter(tag_id__in=duplicate_ids).order_by("created_at", "pk"):
        key = (assignment.content_type_id, assignment.object_id)
        if key in taken:
            assignment.delete()
        else:
            assignment.tag_id = canonical.pk
            assignment.save(update_fields=["tag"])
            taken.add(key)


def _lock_duplicate_groups(Tag: t.Any) -> None:
    """Lock every tag that has a case duplicate, in pk order, for the rest of the migration transaction.

    An insert of a TagAssignment that points at a locked tag waits, then fails its foreign key check
    once the duplicate is deleted. Without the lock, the cascade would delete it silently.
    """
    keys = (
        Tag.objects.annotate(key=Lower("name"))
        .values("key")
        .annotate(n=Count("pk"))
        .filter(n__gt=1)
        .values_list("key", flat=True)
    )
    ids = Tag.objects.annotate(key=Lower("name")).filter(key__in=list(keys)).values_list("pk", flat=True)
    list(Tag.objects.select_for_update().filter(pk__in=list(ids)).order_by("pk").values_list("pk", flat=True))


def merge_case_duplicate_tags(apps: t.Any, schema_editor: t.Any) -> None:
    """Merge case-duplicate tags into one canonical tag per group."""
    Tag = apps.get_model("common", "Tag")
    TagAssignment = apps.get_model("common", "TagAssignment")

    _lock_duplicate_groups(Tag)

    tags = Tag.objects.annotate(key=Lower("name"), usage=Count("assignments")).order_by("-usage", "created_at", "pk")
    groups: dict[str, list[t.Any]] = defaultdict(list)
    for tag in tags:
        groups[tag.key].append(tag)

    for members in groups.values():
        if len(members) < 2:
            continue
        canonical, duplicates = members[0], members[1:]
        duplicate_ids = [dup.pk for dup in duplicates]

        # A tag that old code created after the first lock can join a group unlocked. Locked rows are a no-op.
        list(Tag.objects.select_for_update().filter(pk__in=[m.pk for m in members]).order_by("pk").values_list("pk", flat=True))

        _fill_empty_fields(Tag, canonical, duplicates)
        _repoint_children(Tag, canonical, members)

        _move_assignments(TagAssignment, canonical, duplicate_ids)

        Tag.objects.filter(pk__in=duplicate_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0023_post_deploy_task_run"),
    ]

    operations = [
        migrations.RunPython(merge_case_duplicate_tags, migrations.RunPython.noop),
    ]
