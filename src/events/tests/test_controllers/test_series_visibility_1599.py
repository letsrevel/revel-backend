"""A series takes the visibility of its most visible event, for its own pages and for follows only (#1599).

The ``organization`` fixture is PRIVATE and the ``event`` fixture is a PUBLIC, OPEN event of ``event_series``. Series
passes, discovery and the admin controllers keep the organization rule.
"""

import typing as t
from datetime import timedelta

import orjson
import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries, EventSeriesFollow, Organization, SeriesPass

pytestmark = pytest.mark.django_db


def _detail_urls(series: EventSeries) -> list[str]:
    return [
        reverse("api:get_event_series", kwargs={"series_id": series.pk}),
        reverse(
            "api:get_event_series_by_slugs", kwargs={"org_slug": series.organization.slug, "series_slug": series.slug}
        ),
    ]


@pytest.fixture(autouse=True)
def no_manifest_storage(settings: t.Any) -> None:
    """Rendering an admin page needs a staticfiles manifest; same swap as ``oauth/tests/test_admin.py``."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def private_series_with_public_event(
    organization: Organization, event: Event, event_series: EventSeries
) -> EventSeries:
    assert organization.visibility == Organization.Visibility.PRIVATE
    assert event.visibility == Event.Visibility.PUBLIC and event.event_series_id == event_series.pk
    Event.objects.filter(pk=event.pk).update(start=timezone.now() + timedelta(days=2))  # upcoming, for the lists
    return event_series


@pytest.fixture
def hidden_event(organization: Organization, event_series: EventSeries) -> Event:
    return Event.objects.create(
        organization=organization,
        event_series=event_series,
        name="Hidden",
        slug="hidden",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PRIVATE,
        start=timezone.now() + timedelta(days=3),
        status=Event.EventStatus.OPEN,
    )


class TestSeriesPages:
    def test_a_public_event_makes_the_series_page_visible(
        self, client: Client, nonmember_client: Client, private_series_with_public_event: EventSeries
    ) -> None:
        for http in (client, nonmember_client):
            for url in _detail_urls(private_series_with_public_event):
                response = http.get(url)
                assert response.status_code == 200, (url, response.content)
                assert response.json()["id"] == str(private_series_with_public_event.pk)

    def test_the_series_page_shows_only_minimal_organization_data(
        self, client: Client, private_series_with_public_event: EventSeries, event: Event
    ) -> None:
        data = client.get(_detail_urls(private_series_with_public_event)[0]).json()

        assert set(data["organization"]) <= {"id", "name", "slug", "logo", "logo_thumbnail", "is_verified"} | set(
            client.get(reverse("api:get_event", kwargs={"event_id": event.pk})).json()["organization"].keys()
        )

    def test_no_visible_event_keeps_the_series_hidden(
        self, client: Client, nonmember_client: Client, organization: Organization, hidden_event: Event
    ) -> None:
        series = hidden_event.event_series
        assert series is not None
        for http in (client, nonmember_client):
            for url in _detail_urls(series):
                assert http.get(url).status_code == 404, url

    def test_a_past_public_event_still_links_to_its_series(
        self, client: Client, private_series_with_public_event: EventSeries, event: Event
    ) -> None:
        Event.objects.filter(pk=event.pk).update(start=timezone.now() - timedelta(days=30))

        assert client.get(_detail_urls(private_series_with_public_event)[0]).status_code == 200

    def test_the_event_list_of_the_series_shows_only_visible_events(
        self, client: Client, private_series_with_public_event: EventSeries, event: Event, hidden_event: Event
    ) -> None:
        response = client.get(reverse("api:list_events"), {"event_series": str(private_series_with_public_event.pk)})

        assert response.status_code == 200
        ids = {row["id"] for row in response.json()["results"]}
        assert str(event.pk) in ids
        assert str(hidden_event.pk) not in ids

    def test_the_resources_list_answers_and_keeps_the_resource_rule(
        self, client: Client, private_series_with_public_event: EventSeries
    ) -> None:
        url = reverse("api:list_event_series_resources", kwargs={"series_id": private_series_with_public_event.pk})

        response = client.get(url)

        assert response.status_code == 200
        assert response.json()["results"] == []

    def test_the_detail_query_count_is_constant(
        self, client: Client, private_series_with_public_event: EventSeries
    ) -> None:
        url = _detail_urls(private_series_with_public_event)[0]
        client.get(url)
        with CaptureQueriesContext(connection) as queries:
            assert client.get(url).status_code == 200
        assert len(queries.captured_queries) <= 6, [q["sql"][:120] for q in queries.captured_queries]


class TestPathsThatKeepTheOrganizationRule:
    def test_the_discovery_list_does_not_list_the_series(
        self, client: Client, private_series_with_public_event: EventSeries
    ) -> None:
        response = client.get(reverse("api:list_event_series"))

        assert response.status_code == 200
        assert str(private_series_with_public_event.pk) not in {row["id"] for row in response.json()["results"]}

    def test_the_series_passes_stay_hidden(self, client: Client, private_series_with_public_event: EventSeries) -> None:
        series_pass = SeriesPass.objects.create(
            event_series=private_series_with_public_event, name="Pass", price="10.00", pro_rata_discount="1.00"
        )

        assert (
            client.get(
                reverse("api:list_series_passes", kwargs={"series_id": private_series_with_public_event.pk})
            ).status_code
            == 404
        )
        assert client.get(reverse("api:get_series_pass_quote", kwargs={"pass_id": series_pass.pk})).status_code == 404

    def test_the_series_admin_controller_stays_hidden(
        self, nonmember_client: Client, private_series_with_public_event: EventSeries
    ) -> None:
        url = reverse("api:edit_event_series", kwargs={"series_id": private_series_with_public_event.pk})

        response = nonmember_client.put(url, data=orjson.dumps({"name": "X"}), content_type="application/json")

        assert response.status_code == 404

    def test_the_queryset_rules_differ_only_where_intended(self, private_series_with_public_event: EventSeries) -> None:
        anonymous = AnonymousUser()

        assert not EventSeries.objects.for_user(anonymous).filter(pk=private_series_with_public_event.pk).exists()
        assert (
            not EventSeries.objects.discoverable_for_user(anonymous)
            .filter(pk=private_series_with_public_event.pk)
            .exists()
        )
        assert EventSeries.objects.viewable_for_user(anonymous).filter(pk=private_series_with_public_event.pk).exists()


class TestFollow:
    def test_a_user_can_follow_a_series_seen_through_a_public_event(
        self, nonmember_client: Client, nonmember_user: RevelUser, private_series_with_public_event: EventSeries
    ) -> None:
        url = reverse("api:follow_event_series", kwargs={"series_id": private_series_with_public_event.pk})

        response = nonmember_client.post(url, data=orjson.dumps({}), content_type="application/json")

        assert response.status_code == 201, response.content
        assert EventSeriesFollow.objects.filter(
            user=nonmember_user, event_series=private_series_with_public_event, is_archived=False
        ).exists()
        status = nonmember_client.get(
            reverse("api:get_event_series_follow_status", kwargs={"series_id": private_series_with_public_event.pk})
        )
        assert status.json()["is_following"] is True

    def test_a_hidden_series_cannot_be_followed(self, nonmember_client: Client, hidden_event: Event) -> None:
        series = hidden_event.event_series
        assert series is not None
        url = reverse("api:follow_event_series", kwargs={"series_id": series.pk})

        assert nonmember_client.post(url, data=orjson.dumps({}), content_type="application/json").status_code == 404


class TestDjangoAdmin:
    def test_the_changelist_shows_the_visibility_and_filters_by_it(
        self, superuser: RevelUser, private_series_with_public_event: EventSeries
    ) -> None:
        admin = Client()
        admin.force_login(superuser)
        url = reverse("admin:events_eventseries_changelist")

        response = admin.get(url, {"organization__visibility__exact": Organization.Visibility.PRIVATE})

        assert response.status_code == 200
        assert private_series_with_public_event.name in response.content.decode()

    def test_the_event_count_link_opens_the_events_of_the_series(
        self, superuser: RevelUser, private_series_with_public_event: EventSeries
    ) -> None:
        admin = Client()
        admin.force_login(superuser)
        url = (
            reverse("admin:events_event_changelist") + f"?event_series__id__exact={private_series_with_public_event.pk}"
        )

        assert admin.get(url).status_code == 200

    def test_the_effective_visibility_names_the_reason(
        self, private_series_with_public_event: EventSeries, event: Event, hidden_event: Event
    ) -> None:
        from django.contrib import admin as django_admin

        model_admin: t.Any = django_admin.site._registry[EventSeries]

        assert event.name in model_admin.effective_public_visibility(private_series_with_public_event)
        Event.objects.filter(pk=event.pk).update(visibility=Event.Visibility.PRIVATE)
        assert model_admin.effective_public_visibility(private_series_with_public_event).startswith("Hidden")
        Organization.objects.filter(pk=private_series_with_public_event.organization_id).update(
            visibility=Organization.Visibility.PUBLIC
        )
        private_series_with_public_event.refresh_from_db()
        assert "organization" in model_admin.effective_public_visibility(private_series_with_public_event)
