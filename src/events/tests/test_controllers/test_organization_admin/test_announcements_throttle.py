"""Regression test for #936: sending an announcement after ordinary admin traffic.

``SendAnnouncementThrottle`` used to share the ``user`` cache bucket with the
100/min default throttle, so an organizer who had made ~25 API calls in the last
minute got a 429 on ``/send``.
"""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Announcement, Organization

from .test_announcements_crud import TestAnnouncementCrudFixtures

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _enable_throttling(settings: t.Any) -> None:
    """The local ``.env`` may disable throttling; this test needs it live."""
    settings.DISABLE_THROTTLING = False


class TestSendAnnouncementThrottle(TestAnnouncementCrudFixtures):
    def test_send_succeeds_after_busy_admin_session(
        self, owner_client: Client, org: Organization, org_owner: RevelUser
    ) -> None:
        list_url = reverse("api:list_announcements", kwargs={"slug": org.slug})
        for _ in range(30):
            assert owner_client.get(list_url).status_code == 200

        announcement = Announcement.objects.create(
            organization=org,
            title="After a busy minute",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.DRAFT,
        )
        url = reverse("api:send_announcement", kwargs={"slug": org.slug, "announcement_id": announcement.id})

        response = owner_client.post(url)

        assert response.status_code == 200
        assert response.json()["status"] == "sent"
