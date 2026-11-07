"""Org-admin membership register: manual entries, edits, ending and deletion (#1319)."""

import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.models import FileExport
from common.schema import ErrorDetail
from common.throttling import ExportThrottle, UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import IsOrganizationOwner, OrganizationPermission
from events.service import member_register_admin_service as register_service
from events.service.export.member_register_export import start_member_register_export
from events.utils import get_organization_day_bounds

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    # Session-only, like private notes: no OAuth scope's consent label promises the legal member register
    # (names/emails of people with and without accounts). Not on the app-token surface (oauth test_scope_coverage).
    auth=I18nJWTAuth(),
    tags=["Organization Admin"],
    throttle=WriteThrottle(),
    permissions=[OrganizationPermission("manage_members")],
)
class OrganizationAdminMemberRegisterController(OrganizationAdminBaseController):
    """The organization's legal register of membership periods."""

    def _get_record(self, slug: str, record_id: UUID) -> models.MembershipRecord:
        organization = self.get_one(slug)
        return get_object_or_404(
            models.MembershipRecord.objects.select_related("tier"), pk=record_id, organization=organization
        )

    @route.get(
        "/member-register",
        url_name="list_member_register",
        response=PaginatedResponseSchema[schema.MembershipRecordSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["full_name", "email"])
    def list_member_register(
        self,
        slug: str,
        params: t.Annotated[filters.MembershipRecordFilterSchema, Query(...)],
    ) -> QuerySet[models.MembershipRecord]:
        """List register entries, newest first."""
        organization = self.get_one(slug)
        qs = models.MembershipRecord.objects.filter(organization=organization).select_related("tier")
        qs = params.filter(qs)
        if params.active_on is not None:
            qs = qs.active_during(*get_organization_day_bounds(organization, params.active_on))
        return qs.order_by("-valid_from", "-created_at")

    @route.post(
        "/member-register/export",
        url_name="export_member_register",
        response={202: schema.FileExportSchema},
        throttle=ExportThrottle(),
    )
    def export_member_register(
        self, slug: str, payload: schema.MemberRegisterExportRequestSchema
    ) -> tuple[int, FileExport]:
        """Start an xlsx export of the register; poll ``GET /exports/{id}`` for the file."""
        organization = self.get_one(slug)
        return 202, start_member_register_export(organization, self.user(), payload.active_on)

    @route.get(
        "/member-register/{record_id}",
        url_name="get_member_register_record",
        response={200: schema.MembershipRecordSchema, 404: ErrorDetail},
        throttle=UserDefaultThrottle(),
    )
    def get_member_register_record(self, slug: str, record_id: UUID) -> models.MembershipRecord:
        """Get one register entry."""
        return self._get_record(slug, record_id)

    @route.post(
        "/member-register",
        url_name="create_member_register_record",
        response={201: schema.MembershipRecordSchema, 400: ErrorDetail, 404: ErrorDetail},
    )
    def create_member_register_record(
        self, slug: str, payload: schema.MembershipRecordCreateSchema
    ) -> tuple[int, models.MembershipRecord]:
        """Add a manual register entry for a person without an account."""
        organization = self.get_one(slug)
        record = register_service.create_manual_record(organization, created_by=self.user(), **payload.model_dump())
        return 201, record

    @route.patch(
        "/member-register/{record_id}",
        url_name="update_member_register_record",
        response={200: schema.MembershipRecordSchema, 400: ErrorDetail, 404: ErrorDetail, 409: ErrorDetail},
    )
    def update_member_register_record(
        self, slug: str, record_id: UUID, payload: schema.MembershipRecordUpdateSchema
    ) -> models.MembershipRecord:
        """Edit a register entry. Linked live memberships are edited through member management."""
        record = self._get_record(slug, record_id)
        data = payload.model_dump(exclude_unset=True)
        if not data:
            return record
        return register_service.update_record(record, data)

    @route.post(
        "/member-register/{record_id}/end",
        url_name="end_member_register_record",
        response={200: schema.MembershipRecordSchema, 400: ErrorDetail, 404: ErrorDetail, 409: ErrorDetail},
    )
    def end_member_register_record(
        self, slug: str, record_id: UUID, payload: schema.MembershipRecordEndSchema
    ) -> models.MembershipRecord:
        """End a register entry. For a linked live membership this cancels the member."""
        record = self._get_record(slug, record_id)
        return register_service.end_record(record, reason=payload.reason, valid_until=payload.valid_until)

    @route.delete(
        "/member-register/{record_id}",
        url_name="delete_member_register_record",
        response={204: None, 404: ErrorDetail, 409: ErrorDetail},
        # Owner-only: the register is the organization's legal record.
        permissions=[OrganizationPermission("manage_members"), IsOrganizationOwner()],
    )
    def delete_member_register_record(self, slug: str, record_id: UUID) -> tuple[int, None]:
        """Permanently delete a register entry."""
        register_service.delete_record(self._get_record(slug, record_id))
        return 204, None
