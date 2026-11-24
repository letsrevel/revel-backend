"""Data migration 0024 merges case-duplicate tags (#1517)."""

import typing as t
import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

BEFORE = [("common", "0023_post_deploy_task_run")]
MERGED = [("common", "0024_merge_case_duplicate_tags")]
AFTER = [("common", "0025_tag_common_tag_name_ci_unique")]


def _migrate(targets: list[tuple[str, str]]) -> t.Any:
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


@pytest.mark.django_db(transaction=True)
def test_merge_case_duplicate_tags() -> None:
    """Migrate back to 0023, create case-duplicates, migrate forward and check the merge.

    ``transaction=True`` is required because the test runs schema migrations, which cannot run
    inside the transaction that wraps a normal test. The ``finally`` leaves the DB at the latest migrations.
    """
    try:
        apps = _migrate(BEFORE)
        Tag = apps.get_model("common", "Tag")
        TagAssignment = apps.get_model("common", "TagAssignment")
        ContentType = apps.get_model("contenttypes", "ContentType")
        ct = ContentType.objects.create(app_label="x", model="y")
        now = timezone.now()

        def tag(name: str, age_days: int, **kwargs: t.Any) -> t.Any:
            created = Tag.objects.create(name=name, **kwargs)
            Tag.objects.filter(pk=created.pk).update(created_at=now - timedelta(days=age_days))
            return Tag.objects.get(pk=created.pk)

        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        d, e, f = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        # Group 1: "Fun" has 3 assignments, so it wins over the older and the newer duplicates.
        popular = tag("Fun", 1, description="", color="#111111")
        old = tag("fun", 9, description="old desc", color="#222222", icon="old-icon")
        new = tag("FUN", 0, description="new desc", icon="new-icon")
        child = tag("kid", 5, parent=old)
        for tag_obj, obj in [(popular, a), (popular, b), (popular, f), (old, a), (old, c), (new, d)]:
            TagAssignment.objects.create(tag=tag_obj, content_type=ct, object_id=obj)

        # Group 2: tie on usage, the oldest wins.
        tie_new = tag("Music", 1)
        tie_old = tag("music", 7)
        TagAssignment.objects.create(tag=tie_new, content_type=ct, object_id=e)
        TagAssignment.objects.create(tag=tie_old, content_type=ct, object_id=e)

        # Group 3: the canonical's parent is one of its duplicates.
        parent_dup = tag("Art", 0)
        art = tag("art", 3, parent=parent_dup)
        TagAssignment.objects.create(tag=art, content_type=ct, object_id=a)

        # A unique tag stays untouched.
        solo = tag("solo", 0)

        apps = _migrate(AFTER)
        Tag = apps.get_model("common", "Tag")
        TagAssignment = apps.get_model("common", "TagAssignment")

        assert sorted(Tag.objects.values_list("name", flat=True)) == ["Fun", "art", "kid", "music", "solo"]

        fun = Tag.objects.get(pk=popular.pk)
        assert fun.description == "old desc"  # blank filled from the most used duplicate
        assert fun.color == "#111111"  # non-blank kept
        assert fun.icon == "old-icon"  # most used duplicate first
        assert set(TagAssignment.objects.filter(tag=fun).values_list("object_id", flat=True)) == {a, b, c, d, f}
        assert not Tag.objects.filter(pk__in=[old.pk, new.pk]).exists()
        assert Tag.objects.get(pk=child.pk).parent_id == fun.pk

        assert not Tag.objects.filter(pk=tie_new.pk).exists()
        assert TagAssignment.objects.filter(tag_id=tie_old.pk).count() == 1  # collision row dropped

        assert Tag.objects.get(pk=art.pk).parent_id is None
        assert Tag.objects.filter(pk=solo.pk).exists()

        # Every assignment survives exactly once, on the canonical tag (the tie loser's duplicate row is dropped).
        assert set(TagAssignment.objects.values_list("tag_id", "object_id")) == {
            (popular.pk, a),
            (popular.pk, b),
            (popular.pk, c),
            (popular.pk, d),
            (popular.pk, f),
            (tie_old.pk, e),
            (art.pk, a),
        }
        assert TagAssignment.objects.count() == 7
    finally:
        _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_merge_uses_current_parent_after_an_earlier_group_repointed_it() -> None:
    """A later group must not write back a parent that an earlier group has deleted."""
    try:
        apps = _migrate(BEFORE)
        Tag = apps.get_model("common", "Tag")
        TagAssignment = apps.get_model("common", "TagAssignment")
        ContentType = apps.get_model("contenttypes", "ContentType")
        ct = ContentType.objects.create(app_label="x", model="y")

        music = Tag.objects.create(name="Music")
        music_dup = Tag.objects.create(name="music")
        jazz = Tag.objects.create(name="Jazz", parent=music_dup)
        jazz_dup = Tag.objects.create(name="jazz")
        for obj in (uuid.uuid4(), uuid.uuid4()):
            TagAssignment.objects.create(tag=music, content_type=ct, object_id=obj)
        TagAssignment.objects.create(tag=music_dup, content_type=ct, object_id=uuid.uuid4())
        TagAssignment.objects.create(tag=jazz, content_type=ct, object_id=uuid.uuid4())

        apps = _migrate(AFTER)
        Tag = apps.get_model("common", "Tag")

        assert sorted(Tag.objects.values_list("name", flat=True)) == ["Jazz", "Music"]
        assert not Tag.objects.filter(pk__in=[music_dup.pk, jazz_dup.pk]).exists()
        assert Tag.objects.get(pk=jazz.pk).parent_id == music.pk
        # The deferred FK is checked at commit; check it now as well.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    finally:
        _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_merge_breaks_a_parent_cycle_created_by_repointing() -> None:
    """X.parent=D (duplicate of C) and C.parent=X must not end as X.parent=C and C.parent=X."""
    try:
        apps = _migrate(BEFORE)
        Tag = apps.get_model("common", "Tag")
        TagAssignment = apps.get_model("common", "TagAssignment")
        ContentType = apps.get_model("contenttypes", "ContentType")
        ct = ContentType.objects.create(app_label="x", model="y")

        canonical = Tag.objects.create(name="Fun")
        dup = Tag.objects.create(name="fun")
        x = Tag.objects.create(name="x", parent=dup)
        Tag.objects.filter(pk=canonical.pk).update(parent=x)
        TagAssignment.objects.create(tag=canonical, content_type=ct, object_id=uuid.uuid4())

        apps = _migrate(AFTER)
        Tag = apps.get_model("common", "Tag")

        assert not Tag.objects.filter(pk=dup.pk).exists()
        assert Tag.objects.get(pk=canonical.pk).parent_id == x.pk
        assert Tag.objects.get(pk=x.pk).parent_id is None
    finally:
        _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_merge_locks_the_duplicate_tags_before_reading_their_assignments() -> None:
    """A FOR UPDATE on the tags runs before any assignment read, so a concurrent insert cannot be lost."""
    try:
        apps = _migrate(BEFORE)
        Tag = apps.get_model("common", "Tag")
        TagAssignment = apps.get_model("common", "TagAssignment")
        ContentType = apps.get_model("contenttypes", "ContentType")
        ct = ContentType.objects.create(app_label="x", model="y")
        keep = Tag.objects.create(name="Fun")
        dup = Tag.objects.create(name="fun")
        TagAssignment.objects.create(tag=keep, content_type=ct, object_id=uuid.uuid4())
        TagAssignment.objects.create(tag=dup, content_type=ct, object_id=uuid.uuid4())

        with CaptureQueriesContext(connection) as ctx:
            _migrate(MERGED)

        sqls = [q["sql"] for q in ctx.captured_queries]
        lock = next(i for i, sql in enumerate(sqls) if "FOR UPDATE" in sql and 'FROM "common_tag"' in sql)
        read = next(i for i, sql in enumerate(sqls) if "common_tagassignment" in sql)
        assert lock < read
    finally:
        _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_failed_constraint_is_recovered_by_migrating_back_to_0023() -> None:
    """A case duplicate created after 0024 makes 0025 fail; migrate to 0023, then forward, merges it."""
    try:
        _migrate(BEFORE)
        apps = _migrate(MERGED)
        Tag = apps.get_model("common", "Tag")
        TagAssignment = apps.get_model("common", "TagAssignment")
        ContentType = apps.get_model("contenttypes", "ContentType")
        ct = ContentType.objects.create(app_label="x", model="y")
        a, b = uuid.uuid4(), uuid.uuid4()
        keep = Tag.objects.create(name="Fun")
        TagAssignment.objects.create(tag=keep, content_type=ct, object_id=a)
        # An old instance creates a case variant after 0024 has committed.
        late = Tag.objects.create(name="fun")
        TagAssignment.objects.create(tag=late, content_type=ct, object_id=b)

        with pytest.raises(IntegrityError):
            _migrate(AFTER)

        _migrate(BEFORE)  # 0024 reverses as a no-op; 0025 was never applied
        apps = _migrate(AFTER)
        Tag = apps.get_model("common", "Tag")
        TagAssignment = apps.get_model("common", "TagAssignment")

        assert Tag.objects.count() == 1
        survivor = Tag.objects.get()
        assert set(TagAssignment.objects.filter(tag=survivor).values_list("object_id", flat=True)) == {a, b}
    finally:
        _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
