"""A covered event cannot become stricter, or leave its series, after a series pass covers it (#1598).

The coverage gate (#1332) accepts only PUBLIC events of the pass's series. A later change would let the pass issue
tickets that the event's own rule forbids, so the change is refused. The tier side of the same rule is #1341.
"""

import re
import typing as t

import orjson
import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.forms import modelform_factory
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, EventSeries, SeriesPass
from events.tests.test_series_pass.covered_events import cover

pytestmark = pytest.mark.django_db

T = Event.EventType


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token!s}")  # type: ignore[attr-defined]


def _put(client: Client, event: Event, **fields: object) -> t.Any:
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    return client.put(url, data=orjson.dumps({"name": event.name, **fields}), content_type="application/json")


@pytest.mark.parametrize("stricter", [T.PRIVATE, T.MEMBERS_ONLY])
def test_the_api_refuses_a_stricter_event_type_on_a_covered_event(
    owner_client: Client, series_pass: SeriesPass, stricter: str
) -> None:
    event, _ = cover(series_pass, "covered", days=3)

    response = _put(owner_client, event, event_type=stricter)

    assert response.status_code == 400, response.content
    assert set(response.json()["errors"]) == {"event_type"}
    assert Event.objects.get(pk=event.pk).event_type == T.PUBLIC


def test_the_api_refuses_to_move_a_covered_event_to_another_series(
    owner_client: Client, series_pass: SeriesPass, foreign_series: EventSeries
) -> None:
    event, _ = cover(series_pass, "covered", days=3)

    response = _put(owner_client, event, event_series_id=str(foreign_series.pk))

    assert response.status_code == 400, response.content
    assert set(response.json()["errors"]) == {"event_series"}
    assert Event.objects.get(pk=event.pk).event_series_id == series_pass.event_series_id


def test_a_covered_event_cannot_leave_its_series(series_pass: SeriesPass) -> None:
    event, _ = cover(series_pass, "covered", days=3)
    event.event_series = None

    with pytest.raises(ValidationError) as exc:
        event.full_clean()

    assert set(exc.value.message_dict) == {"event_series"}


def test_an_inactive_pass_still_refuses_the_change(series_pass: SeriesPass) -> None:
    """As on the tier side: a pass made active again does not check its links again."""
    event, _ = cover(series_pass, "covered", days=3)
    SeriesPass.objects.filter(pk=series_pass.pk).update(is_active=False)
    event.event_type = T.MEMBERS_ONLY

    with pytest.raises(ValidationError) as exc:
        event.save()

    assert "event_type" in exc.value.message_dict


def test_an_admin_form_gets_the_error_on_event_type(series_pass: SeriesPass) -> None:
    """A ModelForm, as the Django admin uses, runs the same check and puts the error on the field."""
    event, _ = cover(series_pass, "covered", days=3)
    form_class = modelform_factory(Event, fields=["event_type"])

    form = form_class(data={"event_type": T.PRIVATE}, instance=event)

    assert not form.is_valid()
    assert "event_type" in form.errors


def test_an_event_that_no_pass_covers_can_change(owner_client: Client, series_pass: SeriesPass) -> None:
    event, _ = cover(series_pass, "uncovered", days=3)
    event.series_pass_links.all().delete()

    response = _put(owner_client, event, event_type=T.MEMBERS_ONLY)

    assert response.status_code == 200, response.content
    assert Event.objects.get(pk=event.pk).event_type == T.MEMBERS_ONLY


def test_an_older_mismatched_event_stays_editable(owner_client: Client, series_pass: SeriesPass) -> None:
    """A covered event that is already members-only (a link from before #1332) keeps its other edits."""
    event, _ = cover(series_pass, "legacy", days=3)
    Event.objects.filter(pk=event.pk).update(event_type=T.MEMBERS_ONLY)
    event = Event.objects.get(pk=event.pk)

    response = _put(owner_client, event, name="Renamed", event_type=T.MEMBERS_ONLY)

    assert response.status_code == 200, response.content
    assert Event.objects.get(pk=event.pk).name == "Renamed"


def test_a_covered_event_can_become_public_again(series_pass: SeriesPass) -> None:
    """A looser rule is allowed: an older members-only covered event can open to the public."""
    event, _ = cover(series_pass, "legacy", days=3)
    Event.objects.filter(pk=event.pk).update(event_type=T.MEMBERS_ONLY)
    event = Event.objects.get(pk=event.pk)
    event.event_type = T.PUBLIC

    event.save()

    assert Event.objects.get(pk=event.pk).event_type == T.PUBLIC


def test_a_save_that_does_not_change_the_fields_reads_no_links(series_pass: SeriesPass) -> None:
    """A full save that changes neither field reads the stored row, but not the links."""
    event, _ = cover(series_pass, "covered", days=3)
    event = Event.objects.get(pk=event.pk)
    event.name = "Renamed"

    with CaptureQueriesContext(connection) as queries:
        event.save()

    assert not [q for q in queries.captured_queries if "seriespasstierlink" in q["sql"]]
    assert Event.objects.get(pk=event.pk).name == "Renamed"


# The check's own read: ``values()`` of only the checked columns (other code on save, e.g. signals, reads whole rows).
_COLUMN = r'"events_event"\."(event_type|event_series_id)"( AS "\w+")?'
_CHECK_READ = re.compile(rf'^SELECT {_COLUMN}(, {_COLUMN})? FROM "events_event"')


def _reads_the_checked_fields(queries: CaptureQueriesContext) -> list[str]:
    return [q["sql"] for q in queries.captured_queries if _CHECK_READ.match(q["sql"])]


def test_a_save_that_does_not_write_the_fields_makes_no_extra_query(series_pass: SeriesPass) -> None:
    """``save(update_fields=[...])`` without the two fields (the attendee count, a status change) skips the check."""
    event, _ = cover(series_pass, "covered", days=3)
    event = Event.objects.get(pk=event.pk)
    event.attendee_count = 5

    with CaptureQueriesContext(connection) as queries:
        event.save(update_fields=["attendee_count"])

    assert not _reads_the_checked_fields(queries)
    assert Event.objects.get(pk=event.pk).attendee_count == 5


def test_a_save_of_a_partly_loaded_event_does_not_load_the_fields(series_pass: SeriesPass) -> None:
    """A deferred ``event_type`` or ``event_series`` is not loaded to check it: the save does not write it."""
    event, _ = cover(series_pass, "covered", days=3)
    partial = Event.objects.defer("event_type", "event_series").get(pk=event.pk)
    partial.name = "Renamed"

    with CaptureQueriesContext(connection) as queries:
        partial.save()

    assert not _reads_the_checked_fields(queries)
    assert Event.objects.get(pk=event.pk).name == "Renamed"
