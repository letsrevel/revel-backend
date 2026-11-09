"""Legal register of membership periods (survives account deletion)."""

import typing as t
from datetime import datetime

from django.conf import settings
from django.contrib.gis.db import models
from django.db.models.functions import Upper
from django.utils.translation import gettext_lazy as _

from common.models import TimeStampedModel


# Module-level with distinct names (not nested ``Kind``/``Source``/``EndedReason``): django-ninja dedupes
# OpenAPI components by bare class name, so nested names collide with ``Venue.Kind`` and others (#782).
# Code keeps the ``MembershipRecord.Kind`` idiom through the class-level aliases.
class MembershipRecordKind(models.TextChoices):
    STANDARD = "standard", _("Standard")
    TEMPORARY = "temporary", _("Temporary")


class MembershipRecordEndedReason(models.TextChoices):
    CANCELLED = "cancelled", _("Cancelled")
    EXPELLED = "expelled", _("Expelled")
    REMOVED = "removed", _("Removed")
    REVOKED = "revoked", _("Revoked")
    TIER_CHANGED = "tier_changed", _("Tier changed")


class MembershipRecordSource(models.TextChoices):
    APPLICATION = "application", _("Application")
    STAFF = "staff", _("Staff")
    TOKEN = "token", _("Token")
    SUBSCRIPTION = "subscription", _("Subscription")
    TICKET = "ticket", _("Ticket")
    MANUAL = "manual", _("Manual")
    IMPORT = "import", _("Import")
    BACKFILL = "backfill", _("Backfill")


class MembershipRecordInviteStatus(models.TextChoices):
    NOT_INVITED = "not_invited", _("Not invited")
    INVITED = "invited", _("Invited")
    LINKED = "linked", _("Linked")


class MembershipRecordLinkMethod(models.TextChoices):
    """How an account-less register entry got linked to an account (#1319).

    Not set for entries the access-row mirror creates already linked (there was no link event) nor for
    rows that predate this field.
    """

    INVITE = "invite", _("Invitation")
    AUTO_EMAIL = "auto_email", _("Verified email")
    APPROVAL = "approval", _("Application approval")
    MERGE = "merge", _("Merge")


class RegisterMatchReason(models.TextChoices):
    """Why an account-less register entry may be the same person as an account or a linked entry (#1319)."""

    EMAIL = "email", _("Email")
    NAME = "name", _("Name")
    CLAIM = "claim", _("Claim")


class MembershipRecordQuerySet(models.QuerySet["MembershipRecord"]):
    """Queries over register records."""

    def open(self) -> t.Self:
        """Records with no end date."""
        return self.filter(valid_until__isnull=True)

    def unlinked(self) -> t.Self:
        """Records without an account (they grant nothing) that were not merged into another record."""
        return self.filter(user__isnull=True, merged_into__isnull=True)

    def with_note_flag(self) -> t.Self:
        """Annotate ``note_exists``: a linked entry shows its user's note, an unlinked one its own."""
        from events.models.user_note import OrganizationUserNote  # lazy: user_note references this model by label

        user_note = OrganizationUserNote.objects.filter(
            organization_id=models.OuterRef("organization_id"), user_id=models.OuterRef("user_id")
        )
        record_note = OrganizationUserNote.objects.filter(record_id=models.OuterRef("pk"))
        return self.annotate(
            note_exists=models.Case(
                models.When(user__isnull=False, then=models.Exists(user_note)),
                default=models.Exists(record_note),
                output_field=models.BooleanField(),
            )
        )

    def unmerged(self) -> t.Self:
        """Records that were not merged into a survivor."""
        return self.filter(merged_into__isnull=True)

    def active_on(self, when: datetime) -> t.Self:
        """Non-revoked records covering ``when`` (``valid_from <= when < valid_until``)."""
        return (
            self.unmerged()
            .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
            .filter(valid_from__lte=when)
            .filter(models.Q(valid_until__isnull=True) | models.Q(valid_until__gt=when))
        )

    def active_during(self, start: datetime, end: datetime) -> t.Self:
        """Non-revoked records whose period overlaps the half-open window ``[start, end)``.

        A record overlaps when it starts before ``end`` and has not ended at or before ``start``
        (``valid_from < end`` and ``valid_until`` is null or ``> start``).

        Args:
            start: Inclusive start of the window.
            end: Exclusive end of the window.

        Returns:
            The filtered queryset.
        """
        return (
            self.unmerged()
            .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
            .filter(valid_from__lt=end)
            .filter(models.Q(valid_until__isnull=True) | models.Q(valid_until__gt=start))
            # A zero-length period (valid_until == valid_from, e.g. an instant tier change) covers no time.
            .filter(models.Q(valid_until__isnull=True) | models.Q(valid_until__gt=models.F("valid_from")))
        )


class MembershipRecord(TimeStampedModel):
    """One membership period of one person in one organization.

    A person who joins, leaves and rejoins has several rows. Identity and acceptance evidence are
    snapshotted so the record survives deletion of the account, tier or questionnaire submission.
    """

    Kind = MembershipRecordKind
    EndedReason = MembershipRecordEndedReason
    Source = MembershipRecordSource
    InviteStatus = MembershipRecordInviteStatus
    LinkMethod = MembershipRecordLinkMethod

    organization = models.ForeignKey("events.Organization", on_delete=models.CASCADE, related_name="membership_records")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="membership_records",
    )
    full_name = models.CharField(max_length=255)
    # Snapshot, not input: deliberately not an EmailField so full_clean on save never rejects a stored address.
    email = models.CharField(max_length=254, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.STANDARD)
    tier = models.ForeignKey(
        "events.MembershipTier", on_delete=models.SET_NULL, null=True, blank=True, related_name="records"
    )
    tier_name = models.CharField(max_length=255, blank=True)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    ended_reason = models.CharField(max_length=20, choices=EndedReason.choices, null=True, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    ticket = models.ForeignKey("events.Ticket", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    event = models.ForeignKey("events.Event", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    subscription = models.ForeignKey(
        "events.MembershipSubscription", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    questionnaire_submission = models.ForeignKey(
        "questionnaires.QuestionnaireSubmission", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    questionnaire_name = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_membership_records",
    )

    # Linking (#1319): single-use claim link bound to this record. Never exposed in a schema or admin list.
    invite_token = models.CharField(max_length=64, unique=True, null=True, blank=True)
    invited_at = models.DateTimeField(null=True, blank=True)
    # Set when a claimant already holds an open STANDARD record in the org: staff reconcile it later.
    duplicate_claimant = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # Provenance of the link (#1319): null for unlinked entries, mirror-created ones and rows from before the field.
    linked_via = models.CharField(max_length=20, choices=LinkMethod.choices, null=True, blank=True)
    linked_at = models.DateTimeField(null=True, blank=True)

    # Reconciliation (#1319): an account-less duplicate absorbed into a survivor stays for audit but counts nowhere.
    # SET_NULL: deleting a survivor must not silently delete the absorbed history.
    merged_into = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="absorbed_records"
    )
    # The survivor's ``valid_from`` before the merge, so an unlink can restore it.
    merge_prior_valid_from = models.DateTimeField(null=True, blank=True)

    objects = MembershipRecordQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["organization", "valid_from"]),
            models.Index(fields=["organization", "user"]),
            # ``email__iexact`` is ``UPPER(email) = UPPER(%s)`` on Postgres: this indexes the auto-link lookups.
            models.Index(Upper("email"), name="events_mrec_email_upper_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                # Literal "standard": keep in sync with Kind.STANDARD (a nested Meta cannot see Kind).
                condition=models.Q(valid_until__isnull=True, kind="standard", user__isnull=False),
                name="unique_open_standard_record_per_user",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__isnull=True) | models.Q(valid_until__gte=models.F("valid_from")),
                name="membership_record_valid_range",
            ),
            models.UniqueConstraint(
                fields=["ticket"],
                condition=models.Q(ticket__isnull=False),
                name="unique_membership_record_ticket",
            ),
        ]

    @property
    def invite_status(self) -> MembershipRecordInviteStatus:
        """Whether the record is linked to an account, invited, or neither."""
        if self.user_id is not None:
            return MembershipRecordInviteStatus.LINKED
        if self.invited_at is not None:
            return MembershipRecordInviteStatus.INVITED
        return MembershipRecordInviteStatus.NOT_INVITED

    def __str__(self) -> str:
        return f"{self.full_name} @ {self.organization_id} ({self.valid_from:%Y-%m-%d})"
