import typing as t
import uuid
from uuid import UUID

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import Prefetch, Q
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from accounts.models import RevelUser
from common.fields import MarkdownField
from common.models import TagAssignment, TaggableMixin, TimeStampedModel

from .mixins import (
    LocationMixin,
    LogoCoverValidationMixin,
    SlugFromNameMixin,
    TokenMixin,
    UserRequestMixin,
    VisibilityMixin,
)

ALLOWED_MEMBERSHIP_REQUEST_METHODS = ["telegram", "email", "webform"]  # kept for backwards compatibility


def _validate_membership_request_methods(value: list[str]) -> None:
    pass  # kept for backwards compatibility


class OrganizationQuerySet(models.QuerySet["Organization"]):
    def with_tags(self) -> t.Self:
        """Prefetch tags and related tag objects for max performance."""
        return self.prefetch_related(
            Prefetch(
                "tags",  # the GenericRelation on Event
                queryset=TagAssignment.objects.select_related("tag"),
                to_attr="prefetched_tagassignments",  # Optional: if you want to use a custom attribute
            )
        )

    def with_city(self) -> t.Self:
        """Select the city as well."""
        return self.select_related("city")

    def for_user(self, user: RevelUser | AnonymousUser, allowed_ids: list[UUID] | None = None) -> t.Self:
        """Get queryset for user using a high-performance UNION-based strategy.

        Avoid slow, multi-JOIN queries.
        """
        is_allowed_special = Q(id__in=allowed_ids) if allowed_ids else Q()

        # --- Fast paths for special users ---
        if user.is_superuser or user.is_staff:
            return self.all()
        if user.is_anonymous:
            return self.filter(Q(visibility=Organization.Visibility.PUBLIC) | is_allowed_special)

        # --- "Gather-then-filter" strategy for standard users ---

        # 1. Gather IDs from all distinct sources of visibility.

        # A) Publicly visible organizations
        public_orgs_qs = self.filter(visibility=Organization.Visibility.PUBLIC).values("id")

        # B) Organizations the user owns
        owned_orgs_qs = self.filter(owner=user).values("id")

        # C) Organizations where the user is a staff member
        staff_orgs_qs = self.filter(staff_members=user).values("id")

        # D) Restricted organizations where the user is a member
        member_orgs_qs = self.filter(
            visibility__in=[Organization.Visibility.MEMBERS_ONLY, Organization.Visibility.PRIVATE], members=user
        ).values("id")

        # 2. Combine the querysets of IDs using UNION.
        # This is extremely fast and efficiently handled by the database.
        visible_org_ids_qs = public_orgs_qs.union(owned_orgs_qs, staff_orgs_qs, member_orgs_qs)

        # 3. Filter the main queryset using the gathered IDs.
        # This final query is very fast as it filters on the primary key.
        return self.filter(Q(id__in=visible_org_ids_qs) | is_allowed_special).distinct()


class OrganizationManager(models.Manager["Organization"]):
    def get_queryset(self) -> OrganizationQuerySet:
        """Get base queryset."""
        return OrganizationQuerySet(self.model, using=self._db)

    def for_user(self, user: RevelUser | AnonymousUser, allowed_ids: list[UUID] | None = None) -> OrganizationQuerySet:
        """Get queryset for user."""
        return self.get_queryset().for_user(user, allowed_ids)

    def with_tags(self) -> OrganizationQuerySet:
        """Returns a queryset prefetching tags."""
        return self.get_queryset().with_tags()

    def with_city(self) -> OrganizationQuerySet:
        """Returns a queryset with city."""
        return self.get_queryset().with_city()

    def full(self) -> OrganizationQuerySet:
        """Returns a queryset prefetching the full organizations."""
        return self.get_queryset().with_city().with_tags()


class Organization(
    TaggableMixin, SlugFromNameMixin, TimeStampedModel, VisibilityMixin, LocationMixin, LogoCoverValidationMixin
):
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    description = MarkdownField(null=True, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_organizations",
    )
    staff_members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="staff_organizations",
        through="events.OrganizationStaff",
        blank=True,
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="member_organizations",
        through="events.OrganizationMember",
        blank=True,
    )
    platform_fee_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=settings.DEFAULT_PLATFORM_FEE_PERCENT,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="The percentage platform fee Revel takes on ticket sales for this organization.",
    )
    platform_fee_fixed = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=settings.DEFAULT_PLATFORM_FEE_FIXED,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="The fixed platform fee for this organization.",
    )
    stripe_account_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        unique=True,
        help_text="The Stripe Connect Account ID for this organization.",
    )
    stripe_charges_enabled = models.BooleanField(
        default=False,
    )
    stripe_details_submitted = models.BooleanField(default=False)
    accept_membership_requests = models.BooleanField(default=False)
    contact_email = models.EmailField(blank=True, null=True)
    contact_email_verified = models.BooleanField(default=False)

    objects = OrganizationManager()

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def is_stripe_connected(self) -> bool:
        """Check if the organization has a Stripe account connected."""
        return self.stripe_account_id is not None and self.stripe_charges_enabled and self.stripe_details_submitted


class PermissionMap(BaseModel):
    view_organization_details: bool = True
    create_event: bool = False
    create_event_series: bool = False
    edit_event_series: bool = False
    delete_event_series: bool = False
    edit_event: bool = True
    delete_event: bool = False
    open_event: bool = True
    manage_tickets: bool = True
    close_event: bool = True
    manage_event: bool = True
    check_in_attendees: bool = True
    invite_to_event: bool = True
    edit_organization: bool = False
    manage_members: bool = False
    manage_potluck: bool = False
    create_questionnaire: bool = False
    edit_questionnaire: bool = False
    delete_questionnaire: bool = False
    evaluate_questionnaire: bool = True


class PermissionsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: PermissionMap = Field(default_factory=PermissionMap)
    event_overrides: dict[uuid.UUID, PermissionMap] = Field(default_factory=dict)


def _get_default_permissions() -> dict[str, t.Any]:
    return PermissionsSchema().model_dump(mode="json")


def _validate_permissions(value: dict[str, t.Any]) -> None:
    try:
        PermissionsSchema.model_validate(value)
    except PydanticValidationError as e:
        raise DjangoValidationError(e.errors())


class OrganizationStaff(TimeStampedModel):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="staff_memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_staff_memberships",
    )
    permissions = models.JSONField(
        default=_get_default_permissions,
        blank=True,
        validators=[_validate_permissions],
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="unique_organization_staff")]
        ordering = ["-created_at"]

    def has_permission(self, permission: str, event_id: str | None = None) -> bool:
        """Verify if a user has permission to perform this action."""
        if event_id:
            event_permissions = self.permissions.get("event_overrides", {}).get(event_id, {})
            if event_permissions:
                return bool(event_permissions.get(permission, False))
        return bool(self.permissions.get("default", {}).get(permission, False))


class OrganizationMember(TimeStampedModel):
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

    class Meta:
        ordering = ["-created_at"]


class OrganizationToken(TokenMixin):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="tokens")
    grants_membership = models.BooleanField(default=True)
    grants_staff_status = models.BooleanField(default=False)

    class Meta:
        indexes = [
            # For listing active tokens by organization
            models.Index(fields=["organization", "expires_at"], name="orgtoken_org_expires"),
            # For listing tokens by organization ordered by creation
            models.Index(fields=["organization", "-created_at"], name="orgtoken_org_created"),
        ]
        ordering = ["-created_at"]


class OrganizationMembershipRequest(UserRequestMixin):
    class Status(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="membership_requests")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="unique_organization_user_membership_request",
            )
        ]
        ordering = ["-created_at"]
