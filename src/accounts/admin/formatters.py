"""Display/formatting helpers for ``RevelUserAdmin``.

Pure functions extracted from ``RevelUserAdmin`` so the admin class stays thin
configuration + delegation and the HTML-rendering logic is readable and testable
in isolation. Model imports are kept local to each function to avoid import-time
cycles (mirrors the original methods).
"""

import typing as t

from django.urls import reverse
from django.utils.html import format_html, format_html_join

from common.signing import get_file_url

if t.TYPE_CHECKING:
    from accounts.models import RevelUser


def profile_thumbnail(obj: "RevelUser") -> str:
    """Render the profile picture thumbnail (or initial fallback) for the list view."""
    if url := get_file_url(obj.profile_picture_thumbnail):
        return format_html(
            '<img src="{}" style="width: 32px; height: 32px; border-radius: 50%; object-fit: cover;" />',
            url,
        )
    return format_html(
        '<span style="display: inline-block; width: 32px; height: 32px; '
        "border-radius: 50%; background-color: #e5e7eb; text-align: center; "
        'line-height: 32px; font-size: 14px; color: #6b7280;">{}</span>',
        obj.username[0].upper() if obj.username else "?",
    )


def profile_image_preview(obj: "RevelUser") -> str:
    """Render the profile picture preview for the detail view."""
    if url := get_file_url(obj.profile_picture_preview):
        return format_html(
            '<img src="{}" style="max-width: 200px; max-height: 200px; border-radius: 8px;" />',
            url,
        )
    if url := get_file_url(obj.profile_picture):
        return format_html(
            '<img src="{}" style="max-width: 200px; max-height: 200px; border-radius: 8px;" />',
            url,
        )
    return "No profile picture"


def event_participation(obj: "RevelUser") -> str:
    """Render the user's recent event participation (tickets + RSVPs) as an HTML list.

    Event names are organizer-controlled, so every value goes through
    format_html escaping — never interpolate them into raw HTML.
    """
    from events.models import EventRSVP, Ticket

    tickets = Ticket.objects.filter(user=obj).select_related("event")
    rsvps = EventRSVP.objects.filter(user=obj).select_related("event")

    items = (
        [
            (
                reverse("admin:events_event_change", args=[ticket.event.id]),
                ticket.event.name,
                f"Ticket ({ticket.status})",
            )
            for ticket in tickets[:10]  # Show recent 10
        ]
        + [
            (reverse("admin:events_event_change", args=[rsvp.event.id]), rsvp.event.name, f"RSVP ({rsvp.status})")
            for rsvp in rsvps[:10]  # Show recent 10
        ]
    )
    return format_html(
        "<h4>Event Participation:</h4><ul>{}</ul>",
        format_html_join("", '<li><a href="{}">{}</a> - {}</li>', items),
    )


def organization_participation(obj: "RevelUser") -> str:
    """Render the user's organization roles (owner/staff/member) as an HTML list.

    Organization names are user-controlled, so every value goes through
    format_html escaping — never interpolate them into raw HTML.
    """
    from events.models import Organization, OrganizationMember, OrganizationStaff

    owned_orgs = Organization.objects.filter(owner=obj)
    staff_orgs = OrganizationStaff.objects.filter(user=obj).select_related("organization")
    member_orgs = OrganizationMember.objects.filter(user=obj).select_related("organization")

    items = (
        [(reverse("admin:events_organization_change", args=[org.id]), org.name, "Owner") for org in owned_orgs]
        + [
            (
                reverse("admin:events_organization_change", args=[staff.organization.id]),
                staff.organization.name,
                "Staff",
            )
            for staff in staff_orgs
        ]
        + [
            (
                reverse("admin:events_organization_change", args=[member.organization.id]),
                member.organization.name,
                "Member",
            )
            for member in member_orgs
        ]
    )
    return format_html(
        "<h4>Organization Roles:</h4><ul>{}</ul>",
        format_html_join("", '<li><a href="{}">{}</a> - {}</li>', items),
    )


def impersonate_link(obj: "RevelUser") -> str:
    """Render the impersonation link for eligible (non-staff, non-superuser) users."""
    from django.utils.translation import gettext_lazy as _

    if obj.is_superuser or obj.is_staff:
        return format_html('<span class="text-base-400">—</span>')
    url = reverse("admin:accounts_reveluser_impersonate", args=[obj.pk])
    return format_html(
        '<a href="{}" target="_blank" class="text-primary-600 hover:text-primary-700 '
        'dark:text-primary-500 dark:hover:text-primary-400 text-sm font-medium">{}</a>',
        url,
        _("Impersonate"),
    )
