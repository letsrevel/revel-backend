from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import IsOrganizationStaff, OrganizationPermission
from events.service import venue_service

from .base import OrganizationAdminBaseController


@api_controller("/organization-admin/{slug}", auth=I18nJWTAuth(), tags=["Organization Admin"], throttle=WriteThrottle())
class OrganizationAdminVenuesController(OrganizationAdminBaseController):
    """Organization venue management endpoints.

    Handles venues, sectors, and seats.
    """

    # ---- Venue Management ----

    @route.get(
        "/venues",
        url_name="list_organization_venues",
        response=PaginatedResponseSchema[schema.VenueDetailSchema],
        permissions=[IsOrganizationStaff()],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description", "address"])
    def list_venues(self, slug: str) -> QuerySet[models.Venue]:
        """List all venues for an organization with their sectors.

        Returns paginated list of venues with sectors (no seats).
        Use the sector endpoints to manage seats.
        """
        organization = self.get_one(slug)
        return models.Venue.objects.filter(organization=organization).select_related("city").prefetch_related("sectors")

    @route.post(
        "/venues",
        url_name="create_organization_venue",
        response={201: schema.VenueDetailSchema},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def create_venue(self, slug: str, payload: schema.VenueCreateSchema) -> tuple[int, models.Venue]:
        """Create a new venue for the organization.

        Creates a venue without any sectors. Use the sector endpoints
        to add sectors and seats after creating the venue.
        """
        organization = self.get_one(slug)
        venue = venue_service.create_venue(organization, payload)
        # Refetch with city for response (new venue has no sectors yet)
        return 201, models.Venue.objects.select_related("city").get(pk=venue.pk)

    @route.get(
        "/venues/{venue_id}",
        url_name="get_organization_venue",
        response=schema.VenueDetailSchema,
        permissions=[IsOrganizationStaff()],
        throttle=UserDefaultThrottle(),
    )
    def get_venue(self, slug: str, venue_id: UUID) -> models.Venue:
        """Get venue details including all sectors (without seats).

        Returns the venue with its sectors. To get seat information,
        use the sector detail or seats list endpoints.
        """
        organization = self.get_one(slug)
        return get_object_or_404(
            models.Venue.objects.with_sectors().select_related("city"),
            pk=venue_id,
            organization=organization,
        )

    @route.put(
        "/venues/{venue_id}",
        url_name="update_organization_venue",
        response=schema.VenueDetailSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def update_venue(self, slug: str, venue_id: UUID, payload: schema.VenueUpdateSchema) -> models.Venue:
        """Update venue details.

        Updates venue metadata. Use sector endpoints to manage
        the venue's sectors and seats.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        venue_service.update_venue(venue, payload)
        # Refetch with prefetched sectors for response
        return models.Venue.objects.with_sectors().select_related("city").get(pk=venue.pk)

    @route.delete(
        "/venues/{venue_id}",
        url_name="delete_organization_venue",
        response={204: None},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def delete_venue(self, slug: str, venue_id: UUID) -> tuple[int, None]:
        """Delete a venue and all its sectors and seats.

        This is a destructive operation that removes:
        - The venue itself
        - All sectors belonging to the venue
        - All seats belonging to those sectors

        Events, tickets, and ticket tiers that reference this venue
        will have their venue field set to NULL.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        venue.delete()
        return 204, None

    # ---- Venue Sector Management ----

    @route.get(
        "/venues/{venue_id}/sectors",
        url_name="list_venue_sectors",
        response=list[schema.VenueSectorWithSeatsSchema],
        permissions=[IsOrganizationStaff()],
        throttle=UserDefaultThrottle(),
    )
    def list_sectors(self, slug: str, venue_id: UUID) -> QuerySet[models.VenueSector]:
        """List all sectors for a venue with their seats.

        Returns all sectors with nested seat information.
        Sectors are ordered by display_order, then name.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        return models.VenueSector.objects.filter(venue=venue).prefetch_related("seats")

    @route.post(
        "/venues/{venue_id}/sectors",
        url_name="create_venue_sector",
        response={201: schema.VenueSectorWithSeatsSchema},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def create_sector(
        self, slug: str, venue_id: UUID, payload: schema.VenueSectorCreateSchema
    ) -> tuple[int, models.VenueSector]:
        """Create a new sector for a venue with optional seats.

        Creates a sector and optionally creates seats within it.
        If the sector has a shape and seats have positions,
        positions must be within the shape polygon.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        sector = venue_service.create_sector(venue, payload)
        # Refresh to get prefetched seats
        return 201, models.VenueSector.objects.prefetch_related("seats").get(pk=sector.pk)

    @route.get(
        "/venues/{venue_id}/sectors/{sector_id}",
        url_name="get_venue_sector",
        response=schema.VenueSectorWithSeatsSchema,
        permissions=[IsOrganizationStaff()],
        throttle=UserDefaultThrottle(),
    )
    def get_sector(self, slug: str, venue_id: UUID, sector_id: UUID) -> models.VenueSector:
        """Get sector details including all seats."""
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        return get_object_or_404(
            models.VenueSector.objects.prefetch_related("seats"),
            pk=sector_id,
            venue=venue,
        )

    @route.put(
        "/venues/{venue_id}/sectors/{sector_id}",
        url_name="update_venue_sector",
        response=schema.VenueSectorWithSeatsSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def update_sector(
        self, slug: str, venue_id: UUID, sector_id: UUID, payload: schema.VenueSectorUpdateSchema
    ) -> models.VenueSector:
        """Update a sector's metadata.

        Note: To manage seats, use the dedicated seat endpoints.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        sector = get_object_or_404(models.VenueSector, pk=sector_id, venue=venue)
        venue_service.update_sector(sector, payload)
        return models.VenueSector.objects.prefetch_related("seats").get(pk=sector.pk)

    @route.delete(
        "/venues/{venue_id}/sectors/{sector_id}",
        url_name="delete_venue_sector",
        response={204: None},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def delete_sector(self, slug: str, venue_id: UUID, sector_id: UUID) -> tuple[int, None]:
        """Delete a sector and all its seats.

        Tickets and ticket tiers that reference this sector
        will have their sector field set to NULL.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        sector = get_object_or_404(models.VenueSector, pk=sector_id, venue=venue)
        sector.delete()
        return 204, None

    # ---- Venue Seat Management ----

    @route.post(
        "/venues/{venue_id}/sectors/{sector_id}/seats",
        url_name="bulk_create_venue_seats",
        response={201: list[schema.VenueSeatSchema]},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def bulk_create_seats(
        self, slug: str, venue_id: UUID, sector_id: UUID, payload: schema.VenueSeatBulkCreateSchema
    ) -> tuple[int, list[models.VenueSeat]]:
        """Bulk create seats in a sector.

        If the sector has a shape and seats have positions,
        all positions must be within the shape polygon.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        sector = get_object_or_404(models.VenueSector, pk=sector_id, venue=venue)
        seats = venue_service.bulk_create_seats(sector, payload.seats)
        return 201, seats

    @route.post(
        "/venues/{venue_id}/sectors/{sector_id}/seats/bulk-delete",
        url_name="bulk_delete_venue_seats",
        response={200: dict[str, int]},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def bulk_delete_seats(
        self, slug: str, venue_id: UUID, sector_id: UUID, payload: schema.VenueSeatBulkDeleteSchema
    ) -> dict[str, int]:
        """Bulk delete seats by their labels.

        This operation is atomic - if any seat cannot be deleted (due to having
        active/pending tickets for future events or not existing), no seats will be deleted.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        sector = get_object_or_404(models.VenueSector, pk=sector_id, venue=venue)
        deleted_count = venue_service.bulk_delete_seats(sector, payload.labels)
        return {"deleted": deleted_count}

    @route.put(
        "/venues/{venue_id}/sectors/{sector_id}/seats/bulk-update",
        url_name="bulk_update_venue_seats",
        response=list[schema.VenueSeatSchema],
        permissions=[OrganizationPermission("edit_organization")],
    )
    def bulk_update_seats(
        self, slug: str, venue_id: UUID, sector_id: UUID, payload: schema.VenueSeatBulkUpdateSchema
    ) -> list[models.VenueSeat]:
        """Bulk update seats by their labels.

        This operation is atomic - if any seat cannot be updated (due to not existing
        or position being outside sector shape), no seats will be updated.

        Note: Seat labels cannot be changed. To rename a seat,
        delete it and create a new one with the desired label.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        sector = get_object_or_404(models.VenueSector, pk=sector_id, venue=venue)
        return venue_service.bulk_update_seats(sector, payload.seats)

    @route.put(
        "/venues/{venue_id}/sectors/{sector_id}/seats/by-label/{label}",
        url_name="update_venue_seat",
        response=schema.VenueSeatSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def update_seat(
        self, slug: str, venue_id: UUID, sector_id: UUID, label: str, payload: schema.VenueSeatUpdateSchema
    ) -> models.VenueSeat:
        """Update a specific seat by its label.

        Note: The seat label cannot be changed. To rename a seat,
        delete it and create a new one with the desired label.

        If the sector has a shape and a new position is provided,
        the position must be within the shape polygon.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        sector = get_object_or_404(models.VenueSector, pk=sector_id, venue=venue)
        seat = venue_service.get_seat_by_label(sector, label)
        return venue_service.update_seat(seat, payload, sector_shape=sector.shape)

    @route.delete(
        "/venues/{venue_id}/sectors/{sector_id}/seats/by-label/{label}",
        url_name="delete_venue_seat",
        response={204: None},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def delete_seat(self, slug: str, venue_id: UUID, sector_id: UUID, label: str) -> tuple[int, None]:
        """Delete a specific seat by its label.

        Cannot delete a seat that has active or pending tickets for future events.
        Past tickets (checked_in, cancelled, or for past events) do not block deletion.
        """
        organization = self.get_one(slug)
        venue = get_object_or_404(models.Venue, pk=venue_id, organization=organization)
        sector = get_object_or_404(models.VenueSector, pk=sector_id, venue=venue)
        seat = venue_service.get_seat_by_label(sector, label)
        venue_service.delete_seat(seat)
        return 204, None
