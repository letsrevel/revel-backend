import typing as t
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import MinValueValidator
from django.db.models import Prefetch, Q
from django.utils import timezone
from django.utils.functional import cached_property

from common.fields import MarkdownField
from common.models import TimeStampedModel

from .mixins import VisibilityMixin
from .organization import MembershipTier, OrganizationMember
from .venue import Venue, VenueSeat, VenueSector

if t.TYPE_CHECKING:
    from accounts.models import RevelUser
    from events.models.event import Event

DEFAULT_TICKET_TIER_NAME = "General Admission"


class TicketTierQuerySet(models.QuerySet["TicketTier"]):
    def with_venue_and_sector(self) -> t.Self:
        """Select venue and sector for serialization (not for transactional queries)."""
        return self.select_related("venue", "sector")

    def for_user(self, user: "RevelUser | AnonymousUser") -> t.Self:
        """Return ticket tiers visible to a given user, combining event and tier-level access.

        Membership status handling:
        - CANCELLED users: Treated as if they have no membership (no access to member-only tiers)
        - BANNED users: Inherit banned status from Event.for_user (won't see events at all)
        """
        from .event import Event
        from .invitation import EventInvitation

        qs = self.select_related("event", "event__organization")

        # --- Anonymous User ---
        if user.is_anonymous:
            return qs.filter(
                visibility=TicketTier.Visibility.PUBLIC,
                event__visibility=Event.Visibility.PUBLIC,
                event__status=Event.EventStatus.OPEN,
            )

        if user.is_superuser:
            return qs

        # --- Authenticated User ---
        # 1. Get all events this user is allowed to see. This is the source of truth.
        # Event.for_user already handles banned users (they won't see events from banned orgs)
        visible_event_ids = Event.objects.for_user(user, include_past=True).values_list("id", flat=True)

        # Base filter: only consider tiers on events the user can see.
        base_q = Q(event_id__in=visible_event_ids)

        # If user is owner/staff of the org, they can see all tiers on that event.
        is_owner_or_staff = Q(event__organization__owner=user) | Q(event__organization__staff_members=user)
        # Django staff are not the same as org staff, but we can treat them like superusers here.
        if user.is_staff:
            return qs.filter(base_q).distinct()

        # For regular users, apply tier-specific visibility rules ON TOP of visible events.
        is_public_tier = Q(visibility=TicketTier.Visibility.PUBLIC)

        # Only valid members (not cancelled, not banned) can see member-only tiers
        member_org_ids = (
            OrganizationMember.objects.for_visibility().filter(user=user).values_list("organization_id", flat=True)
        )
        is_member_tier = Q(
            visibility=TicketTier.Visibility.MEMBERS_ONLY,
            event__organization_id__in=member_org_ids,
        )

        invited_event_ids = EventInvitation.objects.filter(user=user).values_list("event_id", flat=True)
        is_private_tier = Q(visibility=TicketTier.Visibility.PRIVATE, event_id__in=invited_event_ids)

        # A regular user can see a tier if it's on a visible event AND...
        # ...it's a public tier, OR
        # ...it's a member tier and they are a valid member, OR
        # ...it's a private tier and they are invited.
        tier_visibility_q = is_public_tier | is_member_tier | is_private_tier

        # Staff/owners of other orgs might also see some of these events if public,
        # so we also explicitly include tiers for events they are staff/owner of.
        final_q = base_q & (tier_visibility_q | is_owner_or_staff)

        return qs.filter(final_q).distinct()

    def for_visible_event(self, event: "Event", user: "RevelUser | AnonymousUser") -> t.Self:
        """Return ticket tiers for an already visibility-checked event.

        Use this when you already have an event that passed visibility checks via Event.for_user().
        This avoids redundant Event.for_user() queries by only applying tier-level visibility rules.

        Args:
            event: An event that already passed visibility checks for this user.
            user: The user to check tier visibility for.

        Returns:
            QuerySet of tiers the user can see for this specific event.
        """
        from .invitation import EventInvitation

        qs = self.filter(event=event)

        # Superusers and Django staff see all tiers
        if not user.is_anonymous and (user.is_superuser or user.is_staff):
            return qs

        # Anonymous users: only public tiers on public events
        if user.is_anonymous:
            return qs.filter(visibility=TicketTier.Visibility.PUBLIC)

        # Check if user is org owner or staff - they see all tiers
        org = event.organization
        is_owner = org.owner_id == user.id
        is_staff = org.staff_members.filter(id=user.id).exists()
        if is_owner or is_staff:
            return qs

        # Regular user: apply tier visibility rules
        is_public_tier = Q(visibility=TicketTier.Visibility.PUBLIC)

        # Member-only tiers: check if user is valid member of this org
        is_member = OrganizationMember.objects.for_visibility().filter(user=user, organization=org).exists()
        is_member_tier = Q(visibility=TicketTier.Visibility.MEMBERS_ONLY) if is_member else Q(pk__isnull=True)

        # Private tiers: check if user is invited to this event
        is_invited = EventInvitation.objects.filter(user=user, event=event).exists()
        is_private_tier = Q(visibility=TicketTier.Visibility.PRIVATE) if is_invited else Q(pk__isnull=True)

        return qs.filter(is_public_tier | is_member_tier | is_private_tier)


class TicketTierManager(models.Manager["TicketTier"]):
    def get_queryset(self) -> TicketTierQuerySet:
        """Get base queryset for ticket tiers."""
        return TicketTierQuerySet(self.model, using=self._db).prefetch_related("restricted_to_membership_tiers")

    def with_venue_and_sector(self) -> TicketTierQuerySet:
        """Return queryset with venue and sector prefetched for API serialization."""
        return self.get_queryset().with_venue_and_sector()

    def for_user(self, user: "RevelUser | AnonymousUser") -> TicketTierQuerySet:
        """Return ticket tiers visible to a given user, combining event and tier-level access."""
        return self.get_queryset().for_user(user)

    def for_visible_event(self, event: "Event", user: "RevelUser | AnonymousUser") -> TicketTierQuerySet:
        """Return ticket tiers for an already visibility-checked event."""
        return self.get_queryset().for_visible_event(event, user)


class TicketTier(TimeStampedModel, VisibilityMixin):
    """The ticket tier.

    Please note:
    if an event is created with requires_ticket=True and no Tiers exist for that event,
    it will be created via signals.
    """

    # PurchasableBy defines who is allowed to purchase tickets for this tier.
    # Note: The choices here ('public', 'members', 'invited') do not directly align with the
    # choices for visibility ('public', 'private', 'members-only', 'staff-only').
    # This is intentional: visibility controls who can see the ticket tier, while purchasable_by
    # controls who can buy tickets. For example, a tier may be visible to all but only purchasable
    # by members or invitees. If you change these choices, ensure you update related business logic.
    class PurchasableBy(models.TextChoices):
        PUBLIC = "public", "General public"
        MEMBERS = "members", "Members only"
        INVITED = "invited", "Invitees only"
        INVITED_AND_MEMBERS = "invited_and_members", "Invited and Members only"

    class PaymentMethod(models.TextChoices):
        ONLINE = "online", "Online"
        OFFLINE = "offline", "Offline"
        AT_THE_DOOR = "at_the_door", "At The Door"
        FREE = "free", "Free"

    class PriceType(models.TextChoices):
        FIXED = "fixed", "Fixed Price"
        PWYC = "pwyc", "Pay What You Can"

    class SeatAssignmentMode(models.TextChoices):
        NONE = "none", "No seat assignment (GA/standing)"
        RANDOM = "random", "Random assignment at purchase"
        USER_CHOICE = "user_choice", "User chooses seat"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="ticket_tiers")
    name = models.CharField(max_length=255, db_index=True)
    visibility = models.CharField(
        choices=VisibilityMixin.Visibility.choices,
        default=VisibilityMixin.Visibility.PUBLIC,
        max_length=20,
        db_index=True,
    )
    payment_method = models.CharField(
        choices=PaymentMethod.choices,
        default=PaymentMethod.OFFLINE,
        max_length=20,
        db_index=True,
    )
    purchasable_by = models.CharField(
        choices=PurchasableBy.choices, max_length=20, db_index=True, default=PurchasableBy.PUBLIC
    )
    description = MarkdownField(null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    price_type = models.CharField(
        choices=PriceType.choices,
        default=PriceType.FIXED,
        max_length=10,
        db_index=True,
        help_text="Whether this tier has a fixed price or allows pay-what-you-can pricing",
    )
    pwyc_min = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Minimum amount for pay-what-you-can pricing",
    )
    pwyc_max = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="Maximum amount for pay-what-you-can pricing (optional)",
    )
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY, help_text="ISO 4217 currency code")
    sales_start_at = models.DateTimeField(
        null=True, blank=True, db_index=True, help_text="When ticket sales begin for this tier"
    )
    sales_end_at = models.DateTimeField(
        null=True, blank=True, db_index=True, help_text="When ticket sales end for this tier"
    )
    total_quantity = models.PositiveIntegerField(default=None, null=True, blank=True)
    quantity_sold = models.PositiveIntegerField(default=0)
    manual_payment_instructions = MarkdownField(null=True, blank=True)
    restricted_to_membership_tiers = models.ManyToManyField(
        MembershipTier,
        related_name="restricted_ticket_tiers",
        blank=True,
        help_text="If set, only members of these tiers can purchase this ticket.",
    )

    # Venue/seating configuration
    venue = models.ForeignKey(
        Venue,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_tiers",
        help_text="Venue for this tier. Must match event venue if event has one.",
    )
    sector = models.ForeignKey(
        VenueSector,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_tiers",
        help_text="Specific sector for this tier.",
    )
    seat_assignment_mode = models.CharField(
        choices=SeatAssignmentMode.choices,
        default=SeatAssignmentMode.NONE,
        max_length=20,
        db_index=True,
        help_text="How seats are assigned for tickets in this tier.",
    )
    max_tickets_per_user = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Override event's max_tickets_per_user for this tier. Null = inherit from event.",
    )

    display_order = models.PositiveIntegerField(default=0, db_index=True)

    objects = TicketTierManager()

    def get_max_tickets_per_user(self) -> int | None:
        """Return tier limit or fall back to event limit.

        Returns:
            The maximum tickets per user for this tier, or None if unlimited.
        """
        if self.max_tickets_per_user is not None:
            return self.max_tickets_per_user
        return self.event.max_tickets_per_user

    def _validate_sales_window(self) -> None:
        """Validate sales window constraints."""
        if self.sales_start_at and self.sales_start_at > self.event.start:
            raise DjangoValidationError(
                {"sales_start_at": "Ticket sales must start before or at the event start time."}
            )
        if self.sales_start_at and self.sales_end_at and self.sales_end_at <= self.sales_start_at:
            raise DjangoValidationError({"sales_end_at": "Ticket sales end time must be after the sales start time."})

    def _validate_pwyc(self) -> None:
        """Validate pay-what-you-can pricing constraints."""
        if self.price_type == self.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_max < self.pwyc_min:
                raise DjangoValidationError(
                    {"pwyc_max": "Maximum pay-what-you-can amount must be greater than or equal to minimum amount."}
                )

    def _validate_membership_tiers(self) -> None:
        """Validate membership tier restrictions."""
        for membership_tier in self.restricted_to_membership_tiers.all():
            if membership_tier.organization_id != self.event.organization_id:
                raise DjangoValidationError(
                    {"restricted_to_tiers": "All linked membership tiers must belong to the event's organization."}
                )
        if self.restricted_to_membership_tiers.exists() and self.purchasable_by not in [
            self.PurchasableBy.MEMBERS,
            self.PurchasableBy.INVITED_AND_MEMBERS,
        ]:
            raise DjangoValidationError(
                {
                    "restricted_to_tiers": "If tickets are restricted to specific tiers, 'Purchasable By' must be set "
                    "to 'Members only' or 'Invited and Members only'."
                }
            )

    def _validate_venue_sector(self) -> None:
        """Validate and auto-fill venue/sector constraints."""
        sector = self.sector  # Fetch once to satisfy mypy
        venue = self.venue

        # Auto-fill venue from sector if sector is set but venue is not
        if sector and not self.venue_id:
            self.venue_id = sector.venue_id
            venue = sector.venue

        if sector and self.venue_id and sector.venue_id != self.venue_id:
            raise DjangoValidationError({"sector": "Sector must belong to the specified venue."})

        # Validate venue belongs to the same organization as the event
        if venue and venue.organization_id != self.event.organization_id:
            raise DjangoValidationError({"venue": "Venue must belong to the same organization as the event."})

        if self.venue_id and self.event.venue_id and self.venue_id != self.event.venue_id:
            raise DjangoValidationError({"venue": "Tier venue must match the event venue."})

        if self.seat_assignment_mode != self.SeatAssignmentMode.NONE and not self.sector_id:
            raise DjangoValidationError({"sector": "A sector is required when seat assignment mode is not 'none'."})

    def clean(self) -> None:
        """Validate sales window, PWYC, membership tier, and venue/sector constraints."""
        super().clean()
        self._validate_sales_window()
        self._validate_pwyc()
        self._validate_membership_tiers()
        self._validate_venue_sector()

    def can_purchase(self) -> bool:
        """Check if the ticket can be purchased."""
        now = timezone.now()
        return (self.sales_start_at is None or self.sales_start_at <= now) and (
            self.sales_end_at is None or self.sales_end_at >= now
        )

    @property
    def total_available(self) -> int | None:
        """Helper property."""
        if self.total_quantity is None:
            return None
        return self.total_quantity - self.quantity_sold

    class Meta:
        ordering = ["event", "display_order", "name"]
        constraints = [models.UniqueConstraint(fields=["event", "name"], name="unique_event_name")]
        indexes = [
            models.Index(
                fields=["id", "event", "payment_method"],
            ),
            models.Index(fields=["event", "display_order"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} for event {self.event.name}"


class TicketQuerySet(models.QuerySet["Ticket"]):
    """Custom queryset for Ticket model with common prefetch patterns."""

    def with_event(self) -> t.Self:
        """Select the related event and its organization."""
        return self.select_related("event", "event__organization")

    def with_tier(self) -> t.Self:
        """Select the related tier with venue/sector and city for N+1 prevention."""
        return self.select_related(
            "tier",
            "tier__venue",
            "tier__venue__city",
            "tier__sector",
        )

    def with_seat(self) -> t.Self:
        """Select the related seat for assigned seating."""
        return self.select_related("seat")

    def with_user(self) -> t.Self:
        """Select the related user."""
        return self.select_related("user")

    def with_payment(self) -> t.Self:
        """Select the related payment (OneToOne reverse relation)."""
        return self.select_related("payment")

    def full(self) -> t.Self:
        """Select all commonly needed related objects for user-facing ticket views."""
        return self.select_related(
            "event",
            "event__organization",
            "event__venue",
            "event__venue__city",
            "tier",
            "tier__venue",
            "tier__venue__city",
            "tier__sector",
            "venue",
            "venue__city",
            "seat",
            "user",
            "payment",
        ).prefetch_related("tier__restricted_to_membership_tiers")

    def with_org_membership(self, organization_id: UUID) -> t.Self:
        """Prefetch user's membership for a specific organization."""
        return self.prefetch_related(
            Prefetch(
                "user__organization_memberships",
                queryset=OrganizationMember.objects.filter(organization_id=organization_id).select_related("tier"),
                to_attr="org_membership_list",
            )
        )


class TicketManager(models.Manager["Ticket"]):
    """Custom manager for Ticket with convenience methods for related object selection."""

    def get_queryset(self) -> TicketQuerySet:
        """Get base queryset."""
        return TicketQuerySet(self.model, using=self._db)

    def with_event(self) -> TicketQuerySet:
        """Returns a queryset with event and organization selected."""
        return self.get_queryset().with_event()

    def with_tier(self) -> TicketQuerySet:
        """Returns a queryset with tier, venue/sector, and city selected."""
        return self.get_queryset().with_tier()

    def with_seat(self) -> TicketQuerySet:
        """Returns a queryset with the seat selected."""
        return self.get_queryset().with_seat()

    def with_user(self) -> TicketQuerySet:
        """Returns a queryset with the user selected."""
        return self.get_queryset().with_user()

    def with_payment(self) -> TicketQuerySet:
        """Returns a queryset with the payment selected."""
        return self.get_queryset().with_payment()

    def full(self) -> TicketQuerySet:
        """Returns a queryset with all related objects selected."""
        return self.get_queryset().full()

    def with_org_membership(self, organization_id: UUID) -> TicketQuerySet:
        """Returns a queryset with org membership prefetched."""
        return self.get_queryset().with_org_membership(organization_id)


class Ticket(TimeStampedModel):
    """A ticket for a specific user to a specific event."""

    class TicketStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        CHECKED_IN = "checked_in", "Checked In"
        CANCELLED = "cancelled", "Cancelled"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="tickets")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tickets")
    status = models.CharField(max_length=20, choices=TicketStatus.choices, default=TicketStatus.ACTIVE, db_index=True)
    tier = models.ForeignKey(TicketTier, on_delete=models.CASCADE, related_name="tickets")
    checked_in_at = models.DateTimeField(null=True, blank=True, editable=False)
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checked_in_tickets",
        editable=False,
    )
    guest_name = models.CharField(
        max_length=255,
        help_text="Name of the ticket holder (may differ from purchasing user).",
    )
    price_paid = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Amount paid per ticket for PWYC offline/at_the_door purchases. "
        "Null for online payments (stored in Payment.amount) or fixed-price tiers (use tier.price).",
    )

    # Venue/seating (denormalized for fast access, validated for consistency)
    venue = models.ForeignKey(
        Venue,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )
    sector = models.ForeignKey(
        VenueSector,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )
    seat = models.ForeignKey(
        VenueSeat,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )

    objects = TicketManager()

    class Meta:
        constraints = [
            # Note: unique_ticket_event_user_tier constraint was removed to allow
            # multi-ticket purchases. Overbooking is now prevented at the service layer
            # by checking max_tickets_per_user limits.
            models.UniqueConstraint(
                fields=["event", "seat"],
                condition=models.Q(seat__isnull=False) & ~models.Q(status="cancelled"),
                name="unique_ticket_event_seat",
            ),
        ]
        ordering = ["-created_at"]

    def _validate_seat(self) -> VenueSector | None:
        """Validate and auto-fill sector from seat. Returns the sector for chaining."""
        seat = self.seat
        sector = self.sector

        if seat:
            if not seat.is_active:
                raise DjangoValidationError({"seat": "Cannot assign an inactive seat."})

            if not self.sector_id:
                self.sector_id = seat.sector_id
                sector = seat.sector
            elif seat.sector_id != self.sector_id:
                raise DjangoValidationError({"seat": "Seat must belong to the specified sector."})

        return sector

    def _validate_sector(self, sector: VenueSector | None) -> None:
        """Validate and auto-fill venue from sector."""
        if sector:
            if not self.venue_id:
                self.venue_id = sector.venue_id
            elif sector.venue_id != self.venue_id:
                raise DjangoValidationError({"sector": "Sector must belong to the specified venue."})

    def clean(self) -> None:
        """Validate and auto-fill venue/sector/seat consistency."""
        super().clean()
        sector = self._validate_seat()
        self._validate_sector(sector)

    def __str__(self) -> str:  # pragma: no cover
        tier_str = f" | {self.tier.name!r}" if self.tier else ""
        return f"Ticket for {self.event.name} for {self.user.username}{tier_str}"

    @cached_property
    def apple_pass_available(self) -> bool:
        """Check if apple pass is available."""
        return bool(
            settings.APPLE_WALLET_PASS_TYPE_ID
            and settings.APPLE_WALLET_TEAM_ID
            and settings.APPLE_WALLET_CERT_PATH
            and settings.APPLE_WALLET_KEY_PATH
            and settings.APPLE_WALLET_WWDR_CERT_PATH
        )


def _get_payment_default_expiry() -> datetime:
    return timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)


class Payment(TimeStampedModel):
    class PaymentStatus(models.TextChoices):
        PENDING = "pending"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        REFUNDED = "refunded"

    # note: we cascade on ticket and user deletion because stripe holds financial records for us/the org
    # this is not THE BEST solution, but it's the simplest to keep local GDPR compliance.
    # In the future, a more complex solution will be proposed
    ticket = models.OneToOneField(Ticket, on_delete=models.CASCADE, related_name="payment")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments")
    # Not unique: batch purchases share the same session_id across multiple tickets
    stripe_session_id = models.CharField(max_length=255, db_index=True)
    stripe_payment_intent_id = models.CharField(
        max_length=255, null=True, blank=True, db_index=True, help_text="Stripe PaymentIntent ID for refund processing"
    )
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    platform_fee = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)
    raw_response = models.JSONField(blank=True, default=dict)  # To store the full webhook event for auditing
    expires_at = models.DateTimeField(default=_get_payment_default_expiry, db_index=True, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["stripe_session_id", "ticket"],
                name="unique_payment_per_session_ticket",
            ),
        ]

    def __str__(self) -> str:
        return f"Payment {self.id} for Ticket {self.ticket.id}"

    def has_expired(self) -> bool:
        """Return whether a payment has expired."""
        return self.expires_at < timezone.now()

    @staticmethod
    def stripe_mode() -> str:
        """Stripe mode."""
        key: str = settings.STRIPE_SECRET_KEY
        return "test" if key.startswith("sk_test_") else "live"

    def stripe_dashboard_url(self) -> str:
        """Return the stripe dashboard URL."""
        mode: str = self.stripe_mode()
        if self.stripe_payment_intent_id:
            return f"https://dashboard.stripe.com/{mode}/payments/{self.stripe_payment_intent_id}"
        return f"https://dashboard.stripe.com/{mode}/checkout/sessions/{self.stripe_session_id}"
