import typing as t

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _

from common.fields import MarkdownField
from common.models import TimeStampedModel
from events.utils import membership_access
from events.utils.membership_access import BANNED_STATUS, ELIGIBLE_STATUSES, VISIBLE_STATUSES, is_visible_status

from .mixins import UserRequestMixin
from .organization import Organization


# Module-level with a distinct name (not a nested ``Kind``): django-ninja dedupes OpenAPI components by
# bare class name (#782). Code keeps the ``MembershipTier.Kind`` idiom through the class-level alias.
class MembershipTierKind(models.TextChoices):
    STANDARD = "standard", _("Standard")
    TEMPORARY = "temporary", _("Temporary")


class MembershipTierQuerySet(models.QuerySet["MembershipTier"]):
    """Queries over membership tiers."""

    def standard(self) -> t.Self:
        """Tiers people can hold as members (everything except TEMPORARY day-membership tiers)."""
        return self.filter(kind=MembershipTierKind.STANDARD)


class MembershipTier(TimeStampedModel):
    """Represents a membership tier within an organization.

    TEMPORARY tiers are register-only day memberships granted by tickets (#1320): they are never held on
    an access row and are offered only by the ticket-tier "grants" picker.
    """

    Kind = MembershipTierKind

    name = models.CharField(max_length=255)
    kind = models.CharField(
        max_length=20,
        choices=Kind.choices,
        default=Kind.STANDARD,
        # db_default: a rolling deploy runs the migration while old code still INSERTs without the column.
        db_default=Kind.STANDARD,
        help_text=_("Temporary tiers are day memberships granted by tickets; they cannot be held or applied for."),
    )
    description = MarkdownField(blank=True, null=True)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="tiers",
    )
    display_order = models.PositiveIntegerField(default=0, db_index=True)
    membership_questionnaire = models.ForeignKey(
        "events.OrganizationQuestionnaire",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tiers",
        # Must be a string literal to avoid circular import — keep in sync with
        # OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP.
        limit_choices_to={"questionnaire_type": "membership"},
        help_text=_("Tier-specific membership questionnaire. NULL = inherit org default."),
    )
    requires_membership_approval = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        help_text=_("Tier-specific manual-approval override. NULL = inherit org.default_requires_membership_approval."),
    )

    objects = MembershipTierQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="unique_organization_tier_name",
            )
        ]
        ordering = ["organization", "display_order", "name"]
        indexes = [
            models.Index(fields=["organization", "display_order"]),
        ]

    def __str__(self) -> str:
        return f"{self.organization.name} - {self.name}"

    def clean(self) -> None:
        """Validate tier-level membership questionnaire references the same organization and is MEMBERSHIP type."""
        super().clean()
        if self.kind == MembershipTierKind.TEMPORARY and (
            self.membership_questionnaire_id or self.requires_membership_approval is not None
        ):
            raise DjangoValidationError(
                _("A temporary tier cannot have a membership questionnaire or an approval requirement.")
            )
        if self.membership_questionnaire_id and self.organization_id:
            # Lazy import to avoid circular dependency: questionnaire.py imports organization.py.
            from .questionnaire import OrganizationQuestionnaire

            oq = self.membership_questionnaire
            if oq is not None and oq.organization_id != self.organization_id:
                raise DjangoValidationError(
                    {"membership_questionnaire": _("Membership questionnaire must belong to the same organization.")}
                )
            if oq is not None and oq.questionnaire_type != OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP:
                raise DjangoValidationError(
                    {"membership_questionnaire": _("Membership questionnaire must be of type MEMBERSHIP.")}
                )


class OrganizationMemberQuerySet(models.QuerySet["OrganizationMember"]):
    """Custom QuerySet for OrganizationMember with membership status filtering."""

    def for_visibility(self) -> t.Self:
        """Memberships that may SEE member content (ACTIVE, PAUSED); see ``events.utils.membership_access``."""
        return self.filter(status__in=VISIBLE_STATUSES)

    def active_only(self) -> t.Self:
        """Return only ACTIVE memberships (for eligibility checks)."""
        return self.filter(status__in=ELIGIBLE_STATUSES)

    def banned_only(self) -> t.Self:
        """Return only BANNED memberships."""
        return self.filter(status=BANNED_STATUS)

    def non_cancelled(self) -> t.Self:
        """Return memberships in any status except CANCELLED."""
        return self.exclude(status=membership_access.CANCELLED_STATUS)


class OrganizationMemberManager(models.Manager["OrganizationMember"]):
    """Manager for OrganizationMember with custom queryset."""

    def get_queryset(self) -> OrganizationMemberQuerySet:
        """Return custom queryset."""
        return OrganizationMemberQuerySet(self.model, using=self._db)

    def for_visibility(self) -> OrganizationMemberQuerySet:
        """Return memberships that grant visibility access."""
        return self.get_queryset().for_visibility()

    def active_only(self) -> OrganizationMemberQuerySet:
        """Return only ACTIVE memberships."""
        return self.get_queryset().active_only()

    def banned_only(self) -> OrganizationMemberQuerySet:
        """Return only BANNED memberships."""
        return self.get_queryset().banned_only()


class OrganizationMember(TimeStampedModel):
    class MembershipStatus(models.TextChoices):
        ACTIVE = "active"
        PAUSED = "paused"
        CANCELLED = "cancelled"
        BANNED = "banned"

    QR_PREFIX: t.ClassVar[str] = "member:"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    status = models.CharField(
        default=MembershipStatus.ACTIVE, choices=MembershipStatus.choices, max_length=255, db_index=True
    )
    tier = models.ForeignKey(
        MembershipTier,
        on_delete=models.SET_NULL,
        related_name="members",
        null=True,
        blank=True,
    )

    objects = OrganizationMemberManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="unique_organization_member_user"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} @ {self.organization_id} ({self.status})"

    def clean(self) -> None:
        """Validate that tier belongs to the same organization and is not a TEMPORARY tier."""
        super().clean()
        if self.tier and self.tier.kind == MembershipTierKind.TEMPORARY:
            raise DjangoValidationError({"tier": _("A temporary tier cannot be held as a membership.")})
        if self.tier and self.tier.organization_id != self.organization_id:
            raise DjangoValidationError({"tier": _("The tier must belong to the same organization as the membership.")})

    @property
    def qr_payload(self) -> str:
        """QR/barcode payload for this membership card.

        Check-in/verification contract: scanners submit this string verbatim, and the
        resolvers strip ``QR_PREFIX`` back off to get the member id — so this is the
        single source of truth every generator (PDF, Apple Wallet, Google Wallet) must use.
        The member row is unique per (organization, user) and reused across
        cancel/rejoin, so the payload is stable for the lifetime of the relationship.
        """
        return f"{self.QR_PREFIX}{self.id}"

    @property
    def _can_hold_card(self) -> bool:
        """Whether this member's status allows holding a membership card.

        Mirrors ``for_visibility()`` / ``MembershipWalletController.get_member``:
        CANCELLED and BANNED members get a 404 from every card download endpoint,
        so availability flags must not advertise a button that would fail.
        """
        return is_visible_status(self.status)

    @property
    def apple_pass_available(self) -> bool:
        """Whether an Apple Wallet membership card is available for this member."""
        from events.utils import apple_wallet_configured

        return apple_wallet_configured() and self._can_hold_card

    @property
    def google_pass_available(self) -> bool:
        """Whether a Google Wallet membership card is available for this member."""
        from events.utils import google_wallet_configured

        return google_wallet_configured() and self._can_hold_card


class MembershipRequestStatus(models.TextChoices):
    """Application state machine for :class:`OrganizationMembershipRequest`.

    Module-level with a distinct class name (not nested as ``Status``):
    django-ninja dedupes OpenAPI ``components.schemas`` by bare class name with
    last-writer-wins, so a nested ``Status`` clobbered the 3-value
    ``UserRequestMixin.Status`` that ``WhitelistRequestSchema`` infers (#782).
    Code keeps using the ``OrganizationMembershipRequest.Status`` idiom via the
    class-level alias.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class OrganizationMembershipRequest(UserRequestMixin):
    # Alias so callers keep the ``Model.Status`` idiom; the class lives at
    # module level under a collision-free name (see its docstring, #782).
    # The subclass deliberately widens the mixin's 3-value Status enum, so the
    # attribute type genuinely differs from the base — hence the ignore.
    Status = MembershipRequestStatus  # type: ignore[assignment]

    # Override the inherited status field to use the expanded choices.
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="membership_requests")
    tier = models.ForeignKey(
        "events.MembershipTier",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="membership_applications",
        help_text=_("Target tier. NULL = tier-less legacy application; staff picks at approval."),
    )
    plan = models.ForeignKey(
        "events.MembershipSubscriptionPlan",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="membership_applications",
        help_text=_("Target plan for the paid path. NULL = free join."),
    )
    subscription = models.ForeignKey(
        "events.MembershipSubscription",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="originating_application",
    )
    questionnaire_submission = models.ForeignKey(
        "questionnaires.QuestionnaireSubmission",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text=_("Submission that satisfied the questionnaire gate, if applicable."),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user", "tier"],
                condition=models.Q(status="pending"),
                nulls_distinct=False,
                name="unique_pending_application_per_user_org_tier",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Membership application: {self.user_id} -> {self.organization_id} ({self.status})"
