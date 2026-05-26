"""Tests for the event bookmark model, manager, and service."""

import typing as t

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError

from accounts.models import RevelUser
from events.models import Event, EventBookmark
from events.service import bookmark_service

pytestmark = pytest.mark.django_db


class TestEventBookmarkModel:
    """Tests for the EventBookmark model."""

    def test_unique_constraint_rejects_duplicate(self, public_event: Event, nonmember_user: RevelUser) -> None:
        """A user cannot bookmark the same event twice."""
        EventBookmark.objects.create(user=nonmember_user, event=public_event)

        with pytest.raises(ValidationError):
            EventBookmark.objects.create(user=nonmember_user, event=public_event)

    def test_str(self, public_event: Event, nonmember_user: RevelUser) -> None:
        """__str__ describes the bookmark."""
        bookmark = EventBookmark.objects.create(user=nonmember_user, event=public_event)
        assert str(public_event) in str(bookmark)
        assert str(nonmember_user) in str(bookmark)


class TestBookmarkService:
    """Tests for bookmark_service."""

    def test_bookmark_event_creates_row(self, public_event: Event, nonmember_user: RevelUser) -> None:
        """bookmark_event creates a bookmark for the user."""
        bookmark = bookmark_service.bookmark_event(nonmember_user, public_event)

        assert bookmark.user == nonmember_user
        assert bookmark.event == public_event
        assert EventBookmark.objects.filter(user=nonmember_user, event=public_event).count() == 1

    def test_bookmark_event_is_idempotent(self, public_event: Event, nonmember_user: RevelUser) -> None:
        """Bookmarking twice returns the same row and does not duplicate."""
        first = bookmark_service.bookmark_event(nonmember_user, public_event)
        second = bookmark_service.bookmark_event(nonmember_user, public_event)

        assert first.pk == second.pk
        assert EventBookmark.objects.filter(user=nonmember_user, event=public_event).count() == 1

    def test_unbookmark_event_hard_deletes(self, public_event: Event, nonmember_user: RevelUser) -> None:
        """unbookmark_event removes the row entirely (hard delete)."""
        bookmark_service.bookmark_event(nonmember_user, public_event)

        bookmark_service.unbookmark_event(nonmember_user, public_event.id)

        assert not EventBookmark.objects.filter(user=nonmember_user, event=public_event).exists()

    def test_unbookmark_event_is_idempotent(self, public_event: Event, nonmember_user: RevelUser) -> None:
        """Unbookmarking a non-bookmarked event is a no-op, not an error."""
        bookmark_service.unbookmark_event(nonmember_user, public_event.id)  # should not raise

        assert not EventBookmark.objects.filter(user=nonmember_user, event=public_event).exists()


class TestWithUserBookmarkAnnotation:
    """Tests for EventQuerySet.with_user_bookmark."""

    def test_annotates_true_when_bookmarked(self, public_event: Event, nonmember_user: RevelUser) -> None:
        """The annotation is True for events the user has bookmarked."""
        EventBookmark.objects.create(user=nonmember_user, event=public_event)

        obj = Event.objects.with_user_bookmark(nonmember_user).get(pk=public_event.pk)

        assert getattr(obj, "user_has_bookmarked") is True

    def test_annotates_false_when_not_bookmarked(self, public_event: Event, nonmember_user: RevelUser) -> None:
        """The annotation is False for events the user has not bookmarked."""
        obj = Event.objects.with_user_bookmark(nonmember_user).get(pk=public_event.pk)

        assert getattr(obj, "user_has_bookmarked") is False

    def test_annotates_false_for_anonymous(self, public_event: Event, nonmember_user: RevelUser) -> None:
        """Anonymous users never have bookmarks; the annotation is a constant False."""
        EventBookmark.objects.create(user=nonmember_user, event=public_event)

        obj = Event.objects.with_user_bookmark(AnonymousUser()).get(pk=public_event.pk)

        assert getattr(obj, "user_has_bookmarked") is False

    def test_annotation_does_not_query_per_row(
        self,
        organization: t.Any,
        nonmember_user: RevelUser,
        django_assert_num_queries: t.Any,
    ) -> None:
        """The annotation is computed in-query, so iterating N events stays one query."""
        from datetime import timedelta

        from django.utils import timezone

        start = timezone.now() + timedelta(days=7)
        events = [
            Event.objects.create(
                organization=organization,
                name=f"Bulk Event {i}",
                slug=f"bulk-event-{i}",
                visibility=Event.Visibility.PUBLIC,
                event_type=Event.EventType.PUBLIC,
                status="open",
                start=start,
                end=start + timedelta(days=1),
            )
            for i in range(3)
        ]
        EventBookmark.objects.create(user=nonmember_user, event=events[0])

        # Materialize the rows up front; the annotation is computed inline in that SELECT.
        rows = list(Event.objects.with_user_bookmark(nonmember_user).filter(pk__in=[e.pk for e in events]))

        # Reading the bookmark state must not trigger any per-row query (no N+1).
        with django_assert_num_queries(0):
            results = {e.pk: getattr(e, "user_has_bookmarked") for e in rows}

        assert results[events[0].pk] is True
        assert results[events[1].pk] is False
        assert results[events[2].pk] is False
