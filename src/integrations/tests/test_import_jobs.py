"""``ImportJob`` lifecycle: the row records why an import failed, and the poll reads only our database."""

import typing as t
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from events.models import Organization
from integrations.enums import IntegrationErrorCode
from integrations.exceptions import IntegrationError, ProviderError
from integrations.models import EventLink, ImportJob, PlatformConnection
from integrations.providers.base import RemoteEvent
from integrations.service import connection_service, import_service, reconcile_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db

START = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)


@pytest.fixture
def connected(organization, organization_owner_user, fake_provider: FakeProvider) -> PlatformConnection:  # type: ignore[no-untyped-def]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    return connection_service.complete_connect(start.state, "c")


@pytest.fixture
def remote(fake_provider: FakeProvider, connected: PlatformConnection) -> str:
    ev = RemoteEvent(name="R", start=START, end=START + timedelta(hours=1), timezone="UTC", currency="EUR")
    return fake_provider.create_event(connected.token(), "acc-1", ev).remote_id


def test_done_job_points_at_the_link(connected: PlatformConnection, remote: str) -> None:
    job = ImportJob.objects.create(connection=connected, remote_id=remote)
    import_service.run_import_job(job)
    job.refresh_from_db()
    assert job.status == ImportJob.Status.DONE
    assert job.link == EventLink.objects.get(connection=connected, remote_id=remote)
    assert job.error_code == ""


def test_provider_error_is_recorded_with_its_code_and_message(connected: PlatformConnection) -> None:
    """A missing remote event is a classified failure: recorded, not raised."""
    job = ImportJob.objects.create(connection=connected, remote_id="gone")
    import_service.run_import_job(job)
    job.refresh_from_db()
    assert job.status == ImportJob.Status.FAILED
    assert job.error_code == IntegrationErrorCode.REMOTE_EVENT_MISSING
    assert job.error_message and job.provider_message == "not found"
    assert not EventLink.objects.exists()


def test_revoked_token_marks_the_connection_revoked(
    connected: PlatformConnection, remote: str, fake_provider: FakeProvider
) -> None:
    fake_provider.fail["get_event"] = ProviderError(IntegrationErrorCode.CONNECTION_REVOKED, "401")
    job = ImportJob.objects.create(connection=connected, remote_id=remote)
    import_service.run_import_job(job)
    job.refresh_from_db()
    connected.refresh_from_db()
    assert job.error_code == IntegrationErrorCode.CONNECTION_REVOKED
    assert connected.status == PlatformConnection.Status.ERROR  # what mark_revoked writes (spec §6.5)


def test_integration_error_is_recorded(
    connected: PlatformConnection, remote: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(*_: t.Any, **__: t.Any) -> EventLink:
        raise IntegrationError(IntegrationErrorCode.ACCOUNT_UNKNOWN, "not yours", status=404)

    monkeypatch.setattr(import_service, "import_remote_event", reject)
    job = ImportJob.objects.create(connection=connected, remote_id=remote)
    import_service.run_import_job(job)
    job.refresh_from_db()
    assert job.status == ImportJob.Status.FAILED
    assert job.error_code == IntegrationErrorCode.ACCOUNT_UNKNOWN and job.error_message == "not yours"


def test_unexpected_exception_is_recorded_and_re_raised(
    connected: PlatformConnection, remote: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: t.Any, **__: t.Any) -> EventLink:
        raise RuntimeError("db went away")

    monkeypatch.setattr(import_service, "import_remote_event", boom)
    job = ImportJob.objects.create(connection=connected, remote_id=remote)
    with pytest.raises(RuntimeError):
        import_service.run_import_job(job)
    job.refresh_from_db()
    assert job.status == ImportJob.Status.FAILED
    assert job.error_code == IntegrationErrorCode.IMPORT_FAILED and job.provider_message == ""


def test_list_import_jobs_is_scoped_to_the_organization_and_provider(
    connected: PlatformConnection, organization: Organization, organization_owner_user: t.Any
) -> None:
    mine = ImportJob.objects.create(connection=connected, remote_id="a")
    other_org = Organization.objects.create(name="Other", slug="other", owner=organization_owner_user)
    other_conn = connection_service.complete_connect(
        connection_service.begin_connect(other_org, organization_owner_user, "fake").state, "c2"
    )
    theirs = ImportJob.objects.create(connection=other_conn, remote_id="b")
    rows = import_service.list_import_jobs(organization, "fake", [mine.id, theirs.id, uuid4()])
    assert [r.id for r in rows] == [mine.id]
    assert rows[0].status == "queued" and rows[0].event_id is None and rows[0].error_code is None
    assert import_service.list_import_jobs(organization, "other-provider", [mine.id]) == []


def test_prune_is_status_blind_and_keeps_recent_jobs(connected: PlatformConnection) -> None:
    old = timezone.now() - timedelta(days=31)
    stale_done = ImportJob.objects.create(connection=connected, remote_id="a", status=ImportJob.Status.DONE)
    stale_queued = ImportJob.objects.create(connection=connected, remote_id="b")  # stranded: never dispatched
    ImportJob.objects.filter(id__in=[stale_done.id, stale_queued.id]).update(created_at=old)
    fresh_failed = ImportJob.objects.create(connection=connected, remote_id="c", status=ImportJob.Status.FAILED)
    assert reconcile_service.prune_import_jobs() == 2
    assert list(ImportJob.objects.values_list("id", flat=True)) == [fresh_failed.id]


def test_request_import_returns_the_job_already_in_flight(
    connected: PlatformConnection, remote: str, organization: Organization
) -> None:
    """A double-clicked "Import" hands back the queued job instead of queuing the event twice."""
    first = import_service.request_import(organization, "fake", [remote])
    second = import_service.request_import(organization, "fake", [remote])
    assert [j.id for j in second.jobs] == [first.jobs[0].id]
    assert ImportJob.objects.filter(connection=connected, remote_id=remote).count() == 1
