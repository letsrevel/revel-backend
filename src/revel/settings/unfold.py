"""Django Unfold admin configuration."""

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from .base import VERSION

# Unfold configuration
UNFOLD = {
    "SITE_TITLE": f"Revel v{VERSION} Admin",
    "SITE_HEADER": f"Revel v{VERSION} Administration",
    "SITE_URL": "/",
    "DASHBOARD_CALLBACK": "revel.dashboard.dashboard_callback",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "COLORS": {
        "primary": {
            "50": "239 246 255",
            "100": "219 234 254",
            "200": "191 219 254",
            "300": "147 197 253",
            "400": "96 165 250",
            "500": "59 130 246",
            "600": "37 99 235",
            "700": "29 78 216",
            "800": "30 64 175",
            "900": "30 58 138",
            "950": "23 37 84",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": _("Dashboard"),
                "separator": False,
                "items": [
                    {
                        "title": _("Dashboard"),
                        "icon": "home",
                        "link": reverse_lazy("admin:index"),
                    },
                ],
            },
            {
                "title": _("Users & Accounts"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Users"),
                        "icon": "person",
                        "link": reverse_lazy("admin:accounts_reveluser_changelist"),
                    },
                    {
                        "title": _("User Data Exports"),
                        "icon": "download",
                        "link": reverse_lazy("admin:accounts_userdataexport_changelist"),
                    },
                ],
            },
            {
                "title": _("Dietary Management"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Dietary Restrictions"),
                        "icon": "block",
                        "link": reverse_lazy("admin:accounts_dietaryrestriction_changelist"),
                    },
                    {
                        "title": _("Dietary Preferences"),
                        "icon": "restaurant",
                        "link": reverse_lazy("admin:accounts_dietarypreference_changelist"),
                    },
                    {
                        "title": _("User Preferences"),
                        "icon": "restaurant_menu",
                        "link": reverse_lazy("admin:accounts_userdietarypreference_changelist"),
                    },
                    {
                        "title": _("Food Items"),
                        "icon": "fastfood",
                        "link": reverse_lazy("admin:accounts_fooditem_changelist"),
                    },
                ],
            },
            {
                "title": _("Organizations"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Organizations"),
                        "icon": "business",
                        "link": reverse_lazy("admin:events_organization_changelist"),
                    },
                    {
                        "title": _("Members"),
                        "icon": "group",
                        "link": reverse_lazy("admin:events_organizationmember_changelist"),
                    },
                    {
                        "title": _("Staff"),
                        "icon": "badge",
                        "link": reverse_lazy("admin:events_organizationstaff_changelist"),
                    },
                    {
                        "title": _("Membership Tiers"),
                        "icon": "workspace_premium",
                        "link": reverse_lazy("admin:events_membershiptier_changelist"),
                    },
                    {
                        "title": _("Membership Requests"),
                        "icon": "how_to_reg",
                        "link": reverse_lazy("admin:events_organizationmembershiprequest_changelist"),
                    },
                    {
                        "title": _("Organization Tokens"),
                        "icon": "vpn_key",
                        "link": reverse_lazy("admin:events_organizationtoken_changelist"),
                    },
                ],
            },
            {
                "title": _("Venues & Seating"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Venues"),
                        "icon": "place",
                        "link": reverse_lazy("admin:events_venue_changelist"),
                    },
                    {
                        "title": _("Venue Sectors"),
                        "icon": "grid_view",
                        "link": reverse_lazy("admin:events_venuesector_changelist"),
                    },
                    {
                        "title": _("Venue Seats"),
                        "icon": "event_seat",
                        "link": reverse_lazy("admin:events_venueseat_changelist"),
                    },
                ],
            },
            {
                "title": _("Events"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Events"),
                        "icon": "event",
                        "link": reverse_lazy("admin:events_event_changelist"),
                    },
                    {
                        "title": _("Event Series"),
                        "icon": "event_repeat",
                        "link": reverse_lazy("admin:events_eventseries_changelist"),
                    },
                    {
                        "title": _("Tickets"),
                        "icon": "confirmation_number",
                        "link": reverse_lazy("admin:events_ticket_changelist"),
                    },
                    {
                        "title": _("Ticket Tiers"),
                        "icon": "sell",
                        "link": reverse_lazy("admin:events_tickettier_changelist"),
                    },
                    {
                        "title": _("RSVPs"),
                        "icon": "how_to_reg",
                        "link": reverse_lazy("admin:events_eventrsvp_changelist"),
                    },
                    {
                        "title": _("Invitations"),
                        "icon": "mail",
                        "link": reverse_lazy("admin:events_eventinvitation_changelist"),
                    },
                    {
                        "title": _("Invitation Requests"),
                        "icon": "mail_outline",
                        "link": reverse_lazy("admin:events_eventinvitationrequest_changelist"),
                    },
                    {
                        "title": _("Waitlists"),
                        "icon": "schedule",
                        "link": reverse_lazy("admin:events_eventwaitlist_changelist"),
                    },
                    {
                        "title": _("Pending Invitations"),
                        "icon": "mail_lock",
                        "link": reverse_lazy("admin:events_pendingeventinvitation_changelist"),
                    },
                    {
                        "title": _("Event Tokens"),
                        "icon": "vpn_key",
                        "link": reverse_lazy("admin:events_eventtoken_changelist"),
                    },
                    {
                        "title": _("Potluck Items"),
                        "icon": "lunch_dining",
                        "link": reverse_lazy("admin:events_potluckitem_changelist"),
                    },
                    {
                        "title": _("Additional Resources"),
                        "icon": "folder",
                        "link": reverse_lazy("admin:events_additionalresource_changelist"),
                    },
                ],
            },
            {
                "title": _("Payments"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Payments"),
                        "icon": "payments",
                        "link": reverse_lazy("admin:events_payment_changelist"),
                    },
                ],
            },
            {
                "title": _("Preferences"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("User Preferences"),
                        "icon": "settings",
                        "link": reverse_lazy("admin:events_generaluserpreferences_changelist"),
                    },
                    {
                        "title": _("Attendee Visibility"),
                        "icon": "visibility",
                        "link": reverse_lazy("admin:events_attendeevisibilityflag_changelist"),
                    },
                ],
            },
            {
                "title": _("Questionnaires"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Questionnaires"),
                        "icon": "quiz",
                        "link": reverse_lazy("admin:questionnaires_questionnaire_changelist"),
                    },
                    {
                        "title": _("Organization Questionnaires"),
                        "icon": "business",
                        "link": reverse_lazy("admin:events_organizationquestionnaire_changelist"),
                    },
                    {
                        "title": _("Submissions"),
                        "icon": "assignment_turned_in",
                        "link": reverse_lazy("admin:questionnaires_questionnairesubmission_changelist"),
                    },
                    {
                        "title": _("Evaluations"),
                        "icon": "grading",
                        "link": reverse_lazy("admin:questionnaires_questionnaireevaluation_changelist"),
                    },
                    {
                        "title": _("Sections"),
                        "icon": "view_module",
                        "link": reverse_lazy("admin:questionnaires_questionnairesection_changelist"),
                    },
                    {
                        "title": _("Multiple Choice Questions"),
                        "icon": "check_box",
                        "link": reverse_lazy("admin:questionnaires_multiplechoicequestion_changelist"),
                    },
                    {
                        "title": _("Free Text Questions"),
                        "icon": "text_fields",
                        "link": reverse_lazy("admin:questionnaires_freetextquestion_changelist"),
                    },
                ],
            },
            {
                "title": _("Notifications"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Notifications"),
                        "icon": "notifications",
                        "link": reverse_lazy("admin:notifications_notification_changelist"),
                    },
                    {
                        "title": _("Notification Deliveries"),
                        "icon": "send",
                        "link": reverse_lazy("admin:notifications_notificationdelivery_changelist"),
                    },
                    {
                        "title": _("Notification Preferences"),
                        "icon": "tune",
                        "link": reverse_lazy("admin:notifications_notificationpreference_changelist"),
                    },
                ],
            },
            {
                "title": _("Geography"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Cities"),
                        "icon": "location_city",
                        "link": reverse_lazy("admin:geo_city_changelist"),
                    },
                ],
            },
            {
                "title": _("Telegram"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Telegram Users"),
                        "icon": "chat",
                        "link": reverse_lazy("admin:telegram_telegramuser_changelist"),
                    },
                ],
            },
            {
                "title": _("System"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Site Settings"),
                        "icon": "settings",
                        "link": reverse_lazy("admin:common_sitesettings_change", args=[1]),
                    },
                    {
                        "title": _("Legal Documents"),
                        "icon": "gavel",
                        "link": reverse_lazy("admin:common_legal_change", args=[1]),
                    },
                    {
                        "title": _("Email Logs"),
                        "icon": "email",
                        "link": reverse_lazy("admin:common_emaillog_changelist"),
                    },
                    {
                        "title": _("Tags"),
                        "icon": "label",
                        "link": reverse_lazy("admin:common_tag_changelist"),
                    },
                    {
                        "title": _("Tag Assignments"),
                        "icon": "label_outline",
                        "link": reverse_lazy("admin:common_tagassignment_changelist"),
                    },
                    {
                        "title": _("File Upload Audits"),
                        "icon": "upload_file",
                        "link": reverse_lazy("admin:common_fileuploadaudit_changelist"),
                    },
                    {
                        "title": _("Quarantined Files"),
                        "icon": "warning",
                        "link": reverse_lazy("admin:common_quarantinedfile_changelist"),
                    },
                ],
            },
            {
                "title": _("Celery Tasks"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Periodic Tasks"),
                        "icon": "schedule",
                        "link": reverse_lazy("admin:django_celery_beat_periodictask_changelist"),
                    },
                    {
                        "title": _("Task Results"),
                        "icon": "task_alt",
                        "link": reverse_lazy("admin:django_celery_results_taskresult_changelist"),
                    },
                ],
            },
            {
                "title": _("Authentication"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Groups"),
                        "icon": "group",
                        "link": reverse_lazy("admin:auth_group_changelist"),
                    },
                ],
            },
        ],
    },
}
