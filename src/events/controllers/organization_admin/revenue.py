"""Org-admin revenue & VAT report endpoints (#551)."""

import datetime as dt
from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.models import FileExport
from common.throttling import ExportThrottle, UserDefaultThrottle
from events import schema
from events.controllers.permissions import OrganizationPermission
from events.schema.export import FileExportSchema
from events.service import revenue_report_service

from .base import OrganizationAdminBaseController


@api_controller("/organization-admin/{slug}", auth=I18nJWTAuth(), tags=["Organization Admin"])
class OrganizationAdminRevenueController(OrganizationAdminBaseController):
    """Generate and poll downloadable revenue & VAT report bundles."""

    @route.post(
        "/revenue-report",
        url_name="create_revenue_report",
        response=FileExportSchema,
        permissions=[OrganizationPermission("manage_organization")],
        throttle=ExportThrottle(),
    )
    def create_revenue_report(
        self, slug: str, payload: schema.RevenueReportRequestSchema, refresh: bool = False
    ) -> FileExport:
        """Create (or reuse a cached) revenue & VAT report for the period."""
        org = self.get_one(slug)
        tz = revenue_report_service.organization_timezone(org)
        today = dt.datetime.now(tz).date()
        scope = revenue_report_service.ReportScope(
            org=org,
            event_id=payload.event_id,
            date_from=payload.date_from or today.replace(month=1, day=1),
            date_to=payload.date_to or today,
        )
        return revenue_report_service.get_or_generate_revenue_report(
            org, scope, requested_by=self.user(), refresh=refresh
        )

    @route.get(
        "/revenue-reports/{export_id}",
        url_name="get_revenue_report",
        response=FileExportSchema,
        permissions=[OrganizationPermission("manage_organization")],
        throttle=UserDefaultThrottle(),
    )
    def get_revenue_report(self, slug: str, export_id: UUID) -> FileExport:
        """Poll a revenue report; includes a signed download URL when READY."""
        org = self.get_one(slug)
        return get_object_or_404(
            FileExport,
            id=export_id,
            export_type=FileExport.ExportType.REVENUE_VAT_REPORT,
            parameters__org_id=str(org.id),
        )
