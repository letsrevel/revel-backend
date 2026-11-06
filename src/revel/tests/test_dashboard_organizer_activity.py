"""Tests for the dashboard's organizer-activity card and the template-free event counts."""

import typing as t
from datetime import datetime

import pytest
from django.test import RequestFactory
from django.utils import timezone
from freezegun import freeze_time

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, Organization
from revel.dashboard import _get_event_analytics, _get_organizer_activity, dashboard_callback

pytestmark = pytest.mark.django_db

# Wednesday 2026-11-04 12:00 Vienna (CET, UTC+1). The week is Mon 2026-11-02 .. Sun 2026-11-08.
NOW = "2026-11-04 11:00:00"


def _local(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return timezone.make_aware(datetime(y, m, d, hh, mm))


def _org(owner: RevelUser, name: str) -> Organization:
    return Organization.objects.create(name=name, slug=name.lower().replace(" ", "-"), owner=owner)


def _event(
    org: Organization,
    name: str,
    created_at: datetime,
    *,
    is_template: bool = False,
    occurrence_index: int | None = None,
) -> Event:
    event = Event.objects.create(
        organization=org,
        name=name,
        slug=name.lower().replace(" ", "-"),
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=100,
        status=Event.EventStatus.DRAFT if is_template else Event.EventStatus.OPEN,
        start=timezone.now(),
        requires_ticket=True,
        is_template=is_template,
        occurrence_index=occurrence_index,
    )
    Event.objects.filter(pk=event.pk).update(created_at=created_at)  # created_at is auto_now_add
    return event


def test_manually_created_excludes_templates_and_occurrences(revel_user_factory: RevelUserFactory) -> None:
    org = _org(revel_user_factory(), "Org")
    now = timezone.now()
    manual = _event(org, "Manual", now)
    _event(org, "Template", now, is_template=True)
    _event(org, "Occurrence", now, occurrence_index=0)

    assert list(Event.objects.manually_created()) == [manual]


@freeze_time(NOW)
def test_week_boundaries(revel_user_factory: RevelUserFactory) -> None:
    org = _org(revel_user_factory(), "Org")
    _event(org, "Sunday late", _local(2026, 11, 8, 23, 59))
    _event(org, "Monday start", _local(2026, 11, 2, 0, 0))
    _event(org, "Next Monday", _local(2026, 11, 9, 0, 0))
    _event(org, "Previous Sunday", _local(2026, 11, 1, 23, 59))

    data = _get_organizer_activity(0)

    assert data["week_start"].isoformat() == "2026-11-02"
    assert data["week_end"].isoformat() == "2026-11-08"
    assert [e["name"] for e in data["orgs"][0]["events"]] == ["Monday start", "Sunday late"]
    assert data["total_events"] == 2
    assert data["total_orgs"] == 1
    assert data["next_offset"] is None
    assert data["prev_offset"] == -1


@freeze_time(NOW)
def test_previous_week_offset(revel_user_factory: RevelUserFactory) -> None:
    org = _org(revel_user_factory(), "Org")
    _event(org, "Last week", _local(2026, 11, 1, 23, 59))
    _event(org, "This week", _local(2026, 11, 2, 0, 0))

    data = _get_organizer_activity(-1)

    assert data["week_start"].isoformat() == "2026-10-26"
    assert [e["name"] for e in data["orgs"][0]["events"]] == ["Last week"]
    assert data["next_offset"] == 0
    assert data["prev_offset"] == -2


@freeze_time(NOW)
def test_grouping_sorting_and_exclusions(revel_user_factory: RevelUserFactory) -> None:
    owner = revel_user_factory()
    alpha, beta, gamma = _org(owner, "Alpha"), _org(owner, "Beta"), _org(owner, "Gamma")
    _event(beta, "B1", _local(2026, 11, 3, 10))
    _event(alpha, "A1", _local(2026, 11, 4, 10))
    _event(gamma, "G2", _local(2026, 11, 5, 10))
    _event(gamma, "G1", _local(2026, 11, 3, 9))
    _event(gamma, "G tpl", _local(2026, 11, 3, 9), is_template=True)
    _event(gamma, "G occ", _local(2026, 11, 3, 9), occurrence_index=1)

    data = _get_organizer_activity(0)

    assert [(o["name"], o["event_count"]) for o in data["orgs"]] == [("Gamma", 2), ("Alpha", 1), ("Beta", 1)]
    assert [e["name"] for e in data["orgs"][0]["events"]] == ["G1", "G2"]
    assert data["total_events"] == 4
    assert data["total_orgs"] == 3


@freeze_time(NOW)
def test_empty_week(superuser: RevelUser) -> None:
    data = _get_organizer_activity(0)
    assert data["orgs"] == []
    assert data["total_events"] == 0


@freeze_time(NOW)
@pytest.mark.parametrize(
    ("param", "expected_start"),
    [
        (None, "2026-11-02"),
        ("-1", "2026-10-26"),
        ("abc", "2026-11-02"),
        ("", "2026-11-02"),
        ("5", "2026-11-02"),
        ("-99999", "2021-11-08"),  # clamped to -260 weeks
    ],
)
def test_week_param_parsing(superuser: RevelUser, param: str | None, expected_start: str) -> None:
    request = RequestFactory().get("/admin/", {"week": param} if param is not None else {})
    request.user = superuser
    context = dashboard_callback(request, {})
    assert context["dashboard"]["organizer_activity"]["week_start"].isoformat() == expected_start


def test_event_counts_exclude_templates(superuser: RevelUser) -> None:
    org = _org(superuser, "Org")
    now = timezone.now()
    _event(org, "Real", now)
    _event(org, "Occ", now, occurrence_index=0)
    _event(org, "Tpl", now, is_template=True)

    request = RequestFactory().get("/admin/")
    request.user = superuser
    context = dashboard_callback(request, {})

    assert t.cast(int, context["dashboard"]["quick_stats"]["total_events"]) == 2
    assert _get_event_analytics()["total"] == 2
