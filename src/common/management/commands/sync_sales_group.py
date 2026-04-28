"""Create or sync the 'Sales' admin group with its permissions.

The Sales group is intended for sales reps who need broad admin access to
onboard organizations, manage events/tickets/discounts, and view financial
records — but not delete data, modify users, or change platform configuration.

Run on each deploy to keep the group's permissions in sync with policy:
    python manage.py sync_sales_group
"""

import typing as t

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

GROUP_NAME = "Sales"

# Full work surface: view + add + change (no delete)
TIER_FULL: tuple[tuple[str, str], ...] = (
    ("events", "organization"),
    ("events", "event"),
    ("events", "eventseries"),
    ("events", "recurrencerule"),
    ("events", "venue"),
    ("events", "tickettier"),
    ("events", "membershiptier"),
    ("events", "eventinvitation"),
    ("events", "pendingeventinvitation"),
    ("events", "eventtoken"),
    ("events", "organizationtoken"),
    ("events", "discountcode"),
    ("common", "tag"),
    ("common", "tagassignment"),
)

# Manage existing records: view + change (no add/delete)
TIER_MANAGE: tuple[tuple[str, str], ...] = (
    ("events", "organizationmember"),
    ("events", "organizationmembershiprequest"),
    ("events", "eventinvitationrequest"),
    ("events", "eventrsvp"),
    ("events", "eventwaitlist"),
    ("events", "ticket"),
    ("events", "potluckitem"),
)

# Read-only surface
TIER_VIEW: tuple[tuple[str, str], ...] = (
    ("accounts", "reveluser"),
    ("accounts", "userbillingprofile"),
    ("accounts", "referralcode"),
    ("accounts", "referral"),
    ("accounts", "referralpayout"),
    ("accounts", "referralpayoutstatement"),
    ("events", "payment"),
    ("events", "attendeeinvoice"),
    ("events", "attendeeinvoicecreditnote"),
    ("events", "platformfeeinvoice"),
    ("events", "platformfeecreditnote"),
    ("events", "blacklist"),
    ("events", "whitelistrequest"),
    ("events", "organizationstaff"),
    ("events", "organizationquestionnaire"),
    ("events", "venuesector"),
    ("events", "venueseat"),
    ("events", "announcement"),
    ("common", "legal"),
    ("common", "sitesettings"),
    ("common", "emaillog"),
    ("common", "exchangerate"),
    ("notifications", "notification"),
    ("notifications", "notificationdelivery"),
    ("notifications", "notificationpreference"),
    ("questionnaires", "questionnaire"),
    ("questionnaires", "questionnairesubmission"),
    ("questionnaires", "questionnaireevaluation"),
    ("questionnaires", "multiplechoicequestion"),
    ("questionnaires", "multiplechoiceoption"),
    ("questionnaires", "freetextquestion"),
    ("questionnaires", "fileuploadquestion"),
    ("questionnaires", "questionnairesection"),
    ("questionnaires", "questionnairefile"),
    ("geo", "city"),
    ("telegram", "telegramuser"),
)


class Command(BaseCommand):
    help = "Create or sync the 'Sales' admin group with its permissions."

    def handle(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Apply the permissions."""
        group, created = Group.objects.get_or_create(name=GROUP_NAME)
        action = "Created" if created else "Found"
        self.stdout.write(f"{action} group: {group.name}")

        permissions: list[Permission] = []
        permissions.extend(self._collect(TIER_FULL, ("view", "add", "change")))
        permissions.extend(self._collect(TIER_MANAGE, ("view", "change")))
        permissions.extend(self._collect(TIER_VIEW, ("view",)))

        group.permissions.set(permissions)
        self.stdout.write(self.style.SUCCESS(f"Synced {group.permissions.count()} permissions on '{group.name}'."))

    def _collect(
        self,
        specs: tuple[tuple[str, str], ...],
        prefixes: tuple[str, ...],
    ) -> list[Permission]:
        perms: list[Permission] = []
        for app_label, model in specs:
            try:
                ct = ContentType.objects.get(app_label=app_label, model=model)
            except ContentType.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"  ! missing content type: {app_label}.{model}"))
                continue
            for prefix in prefixes:
                codename = f"{prefix}_{model}"
                try:
                    perms.append(Permission.objects.get(content_type=ct, codename=codename))
                except Permission.DoesNotExist:
                    self.stdout.write(self.style.WARNING(f"  ! missing permission: {app_label}.{codename}"))
        return perms
