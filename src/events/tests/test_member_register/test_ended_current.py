"""The ``ended`` register filter and the ``is_current`` flag."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.models import MembershipRecord, Organization
from events.schema.member_register import MembershipRecordSchema
from events.utils import get_organization_day_bounds

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


def _names(client: Client, organization: Organization, **params: str) -> set[str]:
    response = client.get(reverse("api:list_member_register", kwargs={"slug": organization.slug}), params)
    assert response.status_code == 200, response.content
    return {r["full_name"] for r in response.json()["results"]}


@pytest.fixture
def data(make_record: RecordFactory) -> None:
    now = timezone.now()
    make_record(full_name="open", valid_from=now - 5 * DAY)
    make_record(
        full_name="day-future-end",
        kind=MembershipRecord.Kind.TEMPORARY,
        valid_from=now - DAY,
        valid_until=now + DAY,
    )
    make_record(full_name="std-past-end", valid_from=now - 9 * DAY, valid_until=now - 2 * DAY)
    make_record(
        full_name="revoked-future",
        valid_from=now - 5 * DAY,
        valid_until=now + 5 * DAY,
        ended_reason=MembershipRecord.EndedReason.REVOKED,
    )
    survivor = make_record(full_name="survivor", valid_from=now - 20 * DAY, valid_until=now - 10 * DAY)
    make_record(full_name="merged-open", valid_from=now - 5 * DAY, merged_into=survivor)
    make_record(full_name="merged-closed", valid_from=now - 9 * DAY, valid_until=now - DAY, merged_into=survivor)


def test_ended_true(owner_client: Client, organization: Organization, data: None) -> None:
    assert _names(owner_client, organization, ended="true") == {"std-past-end", "revoked-future", "survivor"}


def test_ended_false_keeps_null_ended_reason(owner_client: Client, organization: Organization, data: None) -> None:
    assert _names(owner_client, organization, ended="false") == {"open", "day-future-end"}


def test_without_ended_merged_entries_listed_only_when_included(
    owner_client: Client, organization: Organization, data: None
) -> None:
    assert not {"merged-open", "merged-closed"} & _names(owner_client, organization)
    assert {"merged-open", "merged-closed"} <= _names(owner_client, organization, include_merged="true")


def test_ended_combines_with_kind(owner_client: Client, organization: Organization, data: None) -> None:
    assert _names(owner_client, organization, ended="false", kind="temporary") == {"day-future-end"}
    assert _names(owner_client, organization, ended="true", kind="temporary") == set()


class TestIsCurrent:
    def _flag(self, record: MembershipRecord, now: object) -> bool:
        with patch("events.schema.member_register.timezone.now", return_value=now):
            return MembershipRecordSchema.resolve_is_current(record)

    def test_boundaries(self, make_record: RecordFactory) -> None:
        now = timezone.now()
        assert self._flag(make_record(valid_from=now), now) is True
        assert self._flag(make_record(valid_from=now - DAY, valid_until=now), now) is False
        assert self._flag(make_record(valid_from=now - DAY, valid_until=now + DAY), now) is True
        assert self._flag(make_record(valid_from=now + DAY), now) is False

    def test_revoked_and_merged(self, make_record: RecordFactory) -> None:
        now = timezone.now()
        revoked = make_record(valid_from=now - DAY, ended_reason=MembershipRecord.EndedReason.REVOKED)
        assert self._flag(revoked, now) is False
        survivor = make_record(valid_from=now - DAY)
        assert self._flag(make_record(valid_from=now - DAY, merged_into=survivor), now) is False

    def test_in_api(self, owner_client: Client, organization: Organization, make_record: RecordFactory) -> None:
        make_record(full_name="cur", valid_from=timezone.now() - DAY)
        row = owner_client.get(reverse("api:list_member_register", kwargs={"slug": organization.slug})).json()[
            "results"
        ][0]
        assert row["is_current"] is True


class TestActiveAt:
    def test_instant_filter_versus_calendar_day(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        # Deterministic: a fixed day in the organization's timezone, probed at 02:00 local after a 01:00 end.
        day = date(2026, 6, 15)
        day_start, _day_end = get_organization_day_bounds(organization, day)
        probe = day_start + 2 * HOUR
        make_record(full_name="open", valid_from=day_start - 5 * DAY)
        make_record(full_name="ended-hour-ago", valid_from=day_start - 5 * DAY, valid_until=day_start + HOUR)
        make_record(full_name="future", valid_from=probe + HOUR)
        make_record(
            full_name="revoked",
            valid_from=day_start - 5 * DAY,
            valid_until=probe + 5 * DAY,
            ended_reason=MembershipRecord.EndedReason.REVOKED,
        )
        survivor = make_record(full_name="survivor", valid_from=day_start - 20 * DAY, valid_until=day_start - DAY)
        make_record(full_name="merged", valid_from=day_start - 5 * DAY, merged_into=survivor)

        assert _names(owner_client, organization, active_at=probe.isoformat()) == {"open"}
        # The calendar-day filter still includes the entry that ended earlier that day.
        assert _names(owner_client, organization, active_on=day.isoformat()) == {"open", "ended-hour-ago", "future"}

    def test_both_params_is_400(self, owner_client: Client, organization: Organization) -> None:
        response = owner_client.get(
            reverse("api:list_member_register", kwargs={"slug": organization.slug}),
            {"active_on": "2026-06-15", "active_at": timezone.now().isoformat()},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Use either active_on or active_at, not both."
