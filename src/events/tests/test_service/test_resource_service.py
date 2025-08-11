import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events import models
from events.schema import AdditionalResourceCreateSchema, AdditionalResourceUpdateSchema
from events.service import resource_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def another_organization(organization_owner_user: RevelUser) -> models.Organization:
    """A second, distinct organization for testing isolation."""
    return models.Organization.objects.create(name="Another Org", slug="another-org", owner=organization_owner_user)


@pytest.fixture
def another_event(another_organization: models.Organization) -> models.Event:
    """An event belonging to the second organization."""
    return models.Event.objects.create(
        organization=another_organization, name="Another Event", slug="another-event", start=timezone.now()
    )


class TestCreateResource:
    def test_create_resource_success(
        self, organization: models.Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test basic resource creation without any M2M links."""
        payload = AdditionalResourceCreateSchema(
            name="Test Document",
            resource_type=models.AdditionalResource.ResourceTypes.TEXT,
            text="This is the content.",
        )
        resource = resource_service.create_resource(organization, payload)

        assert resource.pk is not None
        assert resource.organization == organization
        assert resource.name == "Test Document"
        assert resource.text == "This is the content."
        assert resource.events.count() == 0

    def test_create_resource_with_valid_m2m(
        self,
        organization: models.Organization,
        organization_owner_user: RevelUser,
        event: models.Event,
        event_series: models.EventSeries,
    ) -> None:
        """Test creating a resource with valid event and event series links."""
        payload = AdditionalResourceCreateSchema(
            name="Linked Document",
            resource_type=models.AdditionalResource.ResourceTypes.LINK,
            link="https://example.com",
            event_ids=[event.id],
            event_series_ids=[event_series.id],
        )
        resource = resource_service.create_resource(organization, payload)

        assert resource.events.count() == 1
        assert resource.events.first() == event
        assert resource.event_series.count() == 1
        assert resource.event_series.first() == event_series

    def test_create_resource_with_invalid_event_id_fails(
        self,
        organization: models.Organization,
        organization_owner_user: RevelUser,
        another_event: models.Event,
    ) -> None:
        """Test that creation fails if linking an event from a different organization."""
        payload = AdditionalResourceCreateSchema(
            name="Invalid Link",
            resource_type=models.AdditionalResource.ResourceTypes.TEXT,
            text="...",
            event_ids=[another_event.id],
        )
        with pytest.raises(HttpError, match="One or more events do not exist or belong to this organization."):
            resource_service.create_resource(organization, payload)

        assert not models.AdditionalResource.objects.exists()


class TestUpdateResource:
    @pytest.fixture
    def resource(self, organization: models.Organization) -> models.AdditionalResource:
        """A pre-existing resource for update tests."""
        return models.AdditionalResource.objects.create(
            organization=organization,
            name="Original Name",
            resource_type=models.AdditionalResource.ResourceTypes.TEXT,
            text="Original text",
        )

    def test_update_resource_basic_fields(self, resource: models.AdditionalResource) -> None:
        """Test updating simple fields of a resource."""
        payload = AdditionalResourceUpdateSchema(name="Updated Name", description="New description.")
        updated = resource_service.update_resource(resource, payload)

        assert updated.name == "Updated Name"
        assert updated.description == "New description."

    def test_update_resource_add_and_replace_m2m(
        self,
        resource: models.AdditionalResource,
        event: models.Event,
        event_series: models.EventSeries,
    ) -> None:
        """Test adding and then replacing M2M links."""
        # Add one event
        payload1 = AdditionalResourceUpdateSchema(event_ids=[event.id])
        resource = resource_service.update_resource(resource, payload1)
        assert list(resource.events.all()) == [event]

        # Add a series and remove the event
        payload2 = AdditionalResourceUpdateSchema(event_ids=[], event_series_ids=[event_series.id])
        resource = resource_service.update_resource(resource, payload2)
        assert resource.events.count() == 0
        assert list(resource.event_series.all()) == [event_series]

    def test_update_resource_with_invalid_series_id_fails(
        self,
        resource: models.AdditionalResource,
        another_organization: models.Organization,
    ) -> None:
        """Test that updating fails if linking a series from another organization."""
        invalid_series = models.EventSeries.objects.create(organization=another_organization, name="Invalid Series")
        payload = AdditionalResourceUpdateSchema(event_series_ids=[invalid_series.id])

        with pytest.raises(HttpError, match="One or more event series do not exist or belong to this organization."):
            resource_service.update_resource(resource, payload)
