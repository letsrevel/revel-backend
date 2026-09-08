"""Org-admin ticket analytics: the org-wide attribution breakdown (#922)."""

import typing as t
from uuid import UUID

from ninja import Query
from ninja_extra import api_controller, route
from pydantic import AwareDatetime

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle
from events import models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import ticket_service

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    auth=I18nJWTAuth(),
    tags=["Organization Admin"],
    permissions=[OrganizationPermission("manage_tickets")],
)
class OrganizationAdminTicketsController(OrganizationAdminBaseController):
    """Ticket analytics across every event of an organization."""

    @route.get(
        "/tickets/attribution",
        url_name="organization_ticket_attribution_breakdown",
        response=list[schema.TicketAttributionBucketSchema],
        throttle=UserDefaultThrottle(),
    )
    def ticket_attribution_breakdown(
        self,
        slug: str,
        since: t.Annotated[AwareDatetime | None, Query()] = None,
        event_ids: t.Annotated[list[UUID] | None, Query()] = None,
    ) -> list[ticket_service.TicketAttributionBucket]:
        """Which campaign tags the organization's tickets were bought through (#922).

        Same shape as the per-event endpoint, summed across every event of the
        organization: one row per distinct `(utm_source, utm_medium, utm_campaign,
        utm_content)` combination with its count of non-cancelled tickets, busiest
        first; the all-`null` row is the *direct* bucket.

        - `since`: only tickets created at or after this instant (compare campaigns
          over a season instead of all time).
        - `event_ids`: restrict to these events of the organization; ids from other
          organizations are ignored.
        """
        org = self.get_one(slug)
        tickets = models.Ticket.objects.filter(event__organization=org)
        if since is not None:
            tickets = tickets.filter(created_at__gte=since)
        if event_ids:
            tickets = tickets.filter(event_id__in=event_ids)
        return ticket_service.attribution_breakdown(tickets)
