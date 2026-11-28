import typing as t
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from common.fields import MarkdownField, ProtectedFileField
from common.models import TimeStampedModel
from events.utils import apple_wallet_configured, get_event_timezone, google_wallet_configured
from events.utils.coverage_lock import lock_row, lock_rows
from events.utils.purchase_rules import tier_rule_stricter_than_pass
from events.utils.tier_pricing import validate_category_prices

from .membership import MembershipTier, MembershipTierKind
from .mixins import VisibilityMixin
from .ticket_number import PreserveTicketNumberMixin
from .ticket_queryset import TicketManager
from .ticket_tier_queryset import TicketTierManager
from .ticket_tier_queryset import TicketTierQuerySet as TicketTierQuerySet  # re-exported: the old import path (#1473)
from .venue import Venue, VenueSeat, VenueSector

if t.TYPE_CHECKING:
    pass

DEFAULT_TICKET_TIER_NAME = "General Admission"


class TicketAttribution(t.TypedDict, total=False):
    """Campaign tags a ticket was bought through (#922).

    The four ``utm_*`` values the embed loader already emits — nothing else
    (no ``utm_term``, no referrer). Sanitised at the API boundary by
    ``events.schema.attribution``; never written from anything but a checkout payload.
    """

    utm_source: str
    utm_medium: str
    utm_campaign: str
    utm_content: str


class CancellationSource(models.TextChoices):
    """Who or what cancelled the ticket."""

    USER = "user", "User self-service"
    ORGANIZER = "organizer", "Organizer via admin"
    STRIPE_DASHBOARD = "stripe_dashboard", "Stripe dashboard"
    EVENT_CANCELLATION = "event_cancellation", "Bulk event cancellation"


class TicketSaleSource(models.TextChoices):
    """How a ticket was issued (#1013), so a comp is not mistaken for a sale on the tier's payment method."""

    CHECKOUT = "checkout", "Checkout"
    BOX_OFFICE_SALE = "box_office_sale", "Box office sale"
    BOX_OFFICE_COMP = "box_office_comp", "Box office comp"
    SERIES_PASS = "series_pass", "Series pass"


class CancellationBlockReason(models.TextChoices):
    """Stable error codes surfaced to the frontend when cancellation is blocked.

    Values are i18n keys — localized copy lives on the frontend.
    """

    ALREADY_CANCELLED = "already_cancelled", "Ticket is already cancelled"
    CHECKED_IN = "checked_in", "Ticket holder has already checked in"
    EVENT_STARTED = "event_started", "Event has already started"
    NOT_PERMITTED = "not_permitted", "Self-service cancellation disabled for this tier"
    PAST_DEADLINE = "past_deadline", "Past the cancellation deadline"
    NOT_OWNER = "not_owner", "Only the ticket holder can cancel this ticket"
    PART_OF_SERIES_PASS = "part_of_series_pass", "Ticket belongs to a series pass"


# Pydantic's JSON dump switches to years ("P1Y…") at 365 days, which Django's parse_duration
# cannot read — so the bound also keeps the API round-trip (model_dump(mode="json")) parseable.
MAX_CHECK_IN_OFFSET = timedelta(days=364)


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
        PUBLIC = "public", _("General public")
        MEMBERS = "members", _("Members only")
        INVITED = "invited", _("Invitees only")
        INVITED_AND_MEMBERS = "invited_and_members", _("Invited and Members only")
        NON_MEMBERS = "non_members", _("Non-members only")

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
        BEST_AVAILABLE = "best_available", "Best available (adjacency-aware)"
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
    sales_paused = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Organizer kill switch: hides the tier from checkout until resumed. Independent of the sales window.",
    )
    check_in_opens_offset = models.DurationField(
        null=True,
        blank=True,
        help_text=(
            "When check-in opens for this tier, relative to the event start (may be negative). "
            "Null = the event's check-in window."
        ),
    )
    check_in_closes_offset = models.DurationField(
        null=True,
        blank=True,
        help_text=(
            "When check-in closes for this tier, relative to the event start. Null = the event's check-in window."
        ),
    )
    total_quantity = models.PositiveIntegerField(default=None, null=True, blank=True)
    quantity_sold = models.PositiveIntegerField(default=0)
    manual_payment_instructions = MarkdownField(null=True, blank=True)
    restricted_to_membership_tiers = models.ManyToManyField(
        MembershipTier,
        related_name="restricted_ticket_tiers",
        blank=True,
        # Admin saves M2Ms after full_clean, so the model check alone would not guard it: limit the picker too.
        limit_choices_to={"kind": MembershipTierKind.STANDARD},
        help_text="If set, only members of these tiers can purchase this ticket.",
    )
    excluded_membership_tiers = models.ManyToManyField(
        MembershipTier,
        related_name="excluded_ticket_tiers",
        blank=True,
        limit_choices_to={"kind": MembershipTierKind.STANDARD},
        help_text="If set with 'Non-members only', only members of these tiers are excluded.",
    )
    grants_membership_tier = models.ForeignKey(
        MembershipTier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granting_ticket_tiers",
        help_text="TEMPORARY membership tier whose day membership a valid ticket of this tier grants (#1320).",
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
    category_prices = models.JSONField(
        default=dict,
        blank=True,
        help_text="{price_category_id: price}. Empty = flat pricing.",
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
        help_text="Additional per-tier cap on tickets per user. Null = no per-tier cap "
        "(the event-level cap still applies).",
    )

    vat_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="VAT rate override for this tier. If null, uses organization's default vat_rate.",
    )
    vat_exemption_reason = models.CharField(max_length=255, blank=True, default="", db_default="")

    display_order = models.PositiveIntegerField(default=0, db_index=True)

    allow_user_cancellation = models.BooleanField(
        default=False,
        help_text="When True, ticket holders can cancel their own tickets under this tier's refund policy.",
    )
    cancellation_deadline_hours = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Users can self-cancel up to this many hours before event start. Null = until event start.",
    )
    refund_policy = models.JSONField(
        null=True,
        blank=True,
        help_text="Serialized RefundPolicy (tiers + flat fee). Null = allow cancellation with zero refund.",
    )

    restrict_visibility_to_linked_invitations = models.BooleanField(
        default=False,
        help_text="When True and visibility=PRIVATE, only users whose invitation links to this tier can see it.",
    )
    restrict_purchase_to_linked_invitations = models.BooleanField(
        default=False,
        help_text=(
            "When True and purchasable_by includes INVITED, "
            "only users whose invitation links to this tier can purchase."
        ),
    )

    objects = TicketTierManager()

    def _validate_sales_window(self) -> None:
        """Validate sales window constraints."""
        if self.sales_start_at and self.sales_start_at > self.event.start:
            raise DjangoValidationError(
                {"sales_start_at": _("Ticket sales must start before or at the event start time.")}
            )
        if self.sales_start_at and self.sales_end_at and self.sales_end_at <= self.sales_start_at:
            raise DjangoValidationError(
                {"sales_end_at": _("Ticket sales end time must be after the sales start time.")}
            )

    def _validate_check_in_window(self) -> None:
        """Validate that the tier's check-in window is non-empty once fallbacks are resolved."""
        if self.check_in_opens_offset is None and self.check_in_closes_offset is None:
            return  # pure event window — Event.clean owns that one
        # Bound before the datetime arithmetic: an absurd offset would raise OverflowError (a 500).
        for field in ("check_in_opens_offset", "check_in_closes_offset"):
            offset = getattr(self, field)
            if offset is not None and abs(offset) > MAX_CHECK_IN_OFFSET:
                raise DjangoValidationError({field: _("Check-in offset must be within 364 days of the event start.")})
        opens_at, closes_at = self.effective_check_in_window()
        if closes_at <= opens_at:
            raise DjangoValidationError(
                {"check_in_closes_offset": _("Check-in end time must be after check-in start time.")}
            )

    def effective_check_in_window(self) -> tuple[datetime, datetime]:
        """Resolve when check-in opens and closes for this tier.

        Each tier offset falls back independently to the event's check-in window, which in
        turn falls back to the event's start/end. Offsets are added in the event's local
        time, so "+1 day" from Saturday 10:00 is Sunday 10:00 even across a DST change.
        """
        event = self.event
        opens_at = event.check_in_starts_at or event.start
        closes_at = event.check_in_ends_at or event.end
        if self.check_in_opens_offset is None and self.check_in_closes_offset is None:
            return opens_at, closes_at  # hot path in tier lists: no city lookup
        # get_event_timezone reads event.city: list querysets that serialize TicketTierSchema
        # (Ticket.objects.full(), the public tier list) select_related it to avoid an N+1.
        # Aware datetime + timedelta within one tzinfo is wall-clock arithmetic (Python does not
        # re-normalise across a DST change for same-zone addition), which is exactly what we want.
        local_start = event.start.astimezone(get_event_timezone(event))
        if self.check_in_opens_offset is not None:
            opens_at = local_start + self.check_in_opens_offset
        if self.check_in_closes_offset is not None:
            closes_at = local_start + self.check_in_closes_offset
        return opens_at, closes_at

    def _validate_pwyc(self) -> None:
        """Validate pay-what-you-can pricing constraints."""
        if self.price_type == self.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_max < self.pwyc_min:
                raise DjangoValidationError(
                    {"pwyc_max": _("Maximum pay-what-you-can amount must be greater than or equal to minimum amount.")}
                )

    def _validate_membership_tiers(self) -> None:
        """Validate membership tier restrictions."""
        for membership_tier in self.restricted_to_membership_tiers.all():
            if membership_tier.organization_id != self.event.organization_id:
                raise DjangoValidationError(
                    {"restricted_to_tiers": _("All linked membership tiers must belong to the event's organization.")}
                )
            if membership_tier.kind == MembershipTier.Kind.TEMPORARY:
                raise DjangoValidationError(
                    {"restricted_to_tiers": _("Temporary membership tiers cannot be used to restrict ticket tiers.")}
                )
        if self.restricted_to_membership_tiers.exists() and self.purchasable_by not in [
            self.PurchasableBy.MEMBERS,
            self.PurchasableBy.INVITED_AND_MEMBERS,
        ]:
            raise DjangoValidationError(
                {
                    "restricted_to_tiers": _(
                        "If tickets are restricted to specific tiers, 'Purchasable By' must be set "
                        "to 'Members only' or 'Invited and Members only'."
                    )
                }
            )
        for membership_tier in self.excluded_membership_tiers.all():
            if membership_tier.organization_id != self.event.organization_id:
                raise DjangoValidationError(
                    {
                        "excluded_membership_tiers": _(
                            "All excluded membership tiers must belong to the event's organization."
                        )
                    }
                )
            if membership_tier.kind == MembershipTier.Kind.TEMPORARY:
                raise DjangoValidationError(
                    {
                        "excluded_membership_tiers": _(
                            "Temporary membership tiers cannot be used to exclude members from ticket tiers."
                        )
                    }
                )
        if self.purchasable_by != self.PurchasableBy.NON_MEMBERS and self.excluded_membership_tiers.exists():
            raise DjangoValidationError(
                {
                    "excluded_membership_tiers": _(
                        "Excluded membership tiers can only be set when 'Purchasable By' is 'Non-members only'."
                    )
                }
            )
        if self.purchasable_by == self.PurchasableBy.NON_MEMBERS and self.visibility == self.Visibility.MEMBERS_ONLY:
            raise DjangoValidationError(
                {"purchasable_by": _("A tier for non-members only cannot be visible to members only.")}
            )

    def clean_fields(self, exclude: t.Collection[str] | None = None) -> None:
        """Skip Django's generic FK check for the grant: ``clean()`` reports a missing tier with one message."""
        super().clean_fields(exclude={*(exclude or ()), "grants_membership_tier"})

    def _validate_grants_membership_tier(self) -> None:
        """Validate the day-membership grant: TEMPORARY tier of this org, one ticket per person, no member gating."""
        if not self.grants_membership_tier_id:
            return
        # Queried, not dereferenced: a nonexistent id must be a ValidationError, not RelatedObjectDoesNotExist.
        granted = MembershipTier.objects.filter(pk=self.grants_membership_tier_id).first()
        if granted is None or granted.organization_id != self.event.organization_id:
            raise DjangoValidationError(
                {"grants_membership_tier": _("The granted membership tier must belong to the event's organization.")}
            )
        if granted.kind != MembershipTier.Kind.TEMPORARY:
            raise DjangoValidationError(
                {"grants_membership_tier": _("Only a temporary tier can be granted by tickets.")}
            )
        if self.purchasable_by in [self.PurchasableBy.MEMBERS, self.PurchasableBy.INVITED_AND_MEMBERS]:
            raise DjangoValidationError(
                {"purchasable_by": _("A ticket tier that grants a membership cannot be restricted to members.")}
            )
        if self.max_tickets_per_user != 1:
            raise DjangoValidationError(
                {
                    "max_tickets_per_user": _(
                        "A ticket tier that grants a membership must be limited to one ticket per person."
                    )
                }
            )
        # No check against restricted_to_membership_tiers: those already require MEMBERS/INVITED_AND_MEMBERS,
        # which the purchasable_by rule above rejects.

    def _validate_venue_sector(self) -> None:
        """Validate and auto-fill venue/sector constraints."""
        sector = self.sector  # Fetch once to satisfy mypy
        venue = self.venue

        # Auto-fill venue from sector if sector is set but venue is not
        if sector and not self.venue_id:
            self.venue_id = sector.venue_id
            venue = sector.venue

        if sector and self.venue_id and sector.venue_id != self.venue_id:
            raise DjangoValidationError({"sector": _("Sector must belong to the specified venue.")})

        # Validate venue belongs to the same organization as the event
        if venue and venue.organization_id != self.event.organization_id:
            raise DjangoValidationError({"venue": _("Venue must belong to the same organization as the event.")})

        if self.venue_id and self.event.venue_id and self.venue_id != self.event.venue_id:
            raise DjangoValidationError({"venue": _("Tier venue must match the event venue.")})

        self._validate_seat_assignment_mode(sector)

    def _validate_seat_assignment_mode(self, sector: "VenueSector | None") -> None:
        """Validate that each seat-assigned mode comes with the field it reads.

        Both seated modes are confined to a sector: USER_CHOICE assigns within it,
        and BEST_AVAILABLE draws its pool from it (narrowed to the zone the buyer
        requests). A category painted across two sectors must never yield a
        cross-sector pool — and neither mode can sit on a standing sector, which
        has no seats to assign at all.
        """
        if self.seat_assignment_mode == self.SeatAssignmentMode.BEST_AVAILABLE and not self.sector_id:
            raise DjangoValidationError(
                {"seat_assignment_mode": _("A sector is required for best-available seat assignment.")}
            )
        if self.seat_assignment_mode == self.SeatAssignmentMode.USER_CHOICE and not self.sector_id:
            raise DjangoValidationError(
                {"seat_assignment_mode": _("A sector is required for user-choice seat assignment.")}
            )
        # A standing sector has no seats to choose from or pick — every hold/checkout
        # would 409. The create/update schemas only see sector_id (no DB access), so
        # this model-level check is the single source of truth for the rule.
        if (
            self.seat_assignment_mode == self.SeatAssignmentMode.USER_CHOICE
            and sector is not None
            and sector.kind == VenueSector.Kind.STANDING
        ):
            raise DjangoValidationError(
                {"seat_assignment_mode": _("User-choice seat assignment requires a seated sector, not a standing one.")}
            )
        if (
            self.seat_assignment_mode == self.SeatAssignmentMode.BEST_AVAILABLE
            and sector is not None
            and sector.kind == VenueSector.Kind.STANDING
        ):
            raise DjangoValidationError(
                {
                    "seat_assignment_mode": _(
                        "Best-available seat assignment requires a seated sector, not a standing one."
                    )
                }
            )

    def _validate_invitation_restrictions(self) -> None:
        """Validate that invitation restriction flags match visibility/purchasable_by settings."""
        if self.restrict_visibility_to_linked_invitations and self.visibility != self.Visibility.PRIVATE:
            raise DjangoValidationError(
                {
                    "restrict_visibility_to_linked_invitations": _(
                        "This option is only valid when visibility is set to 'Private'."
                    )
                }
            )
        if self.restrict_purchase_to_linked_invitations and self.purchasable_by not in [
            self.PurchasableBy.INVITED,
            self.PurchasableBy.INVITED_AND_MEMBERS,
        ]:
            raise DjangoValidationError(
                {
                    "restrict_purchase_to_linked_invitations": _(
                        "This option is only valid when purchasable by is set to "
                        "'Invitees only' or 'Invited and Members only'."
                    )
                }
            )

    def clean(self) -> None:
        """Validate the tier's rules.

        Covers sales and check-in windows, PWYC, membership tier, venue/sector, category prices
        and invitation restrictions.
        """
        super().clean()
        self._validate_sales_window()
        self._validate_check_in_window()
        self._validate_pwyc()
        self._validate_membership_tiers()
        self._validate_grants_membership_tier()
        self._validate_venue_sector()
        validate_category_prices(self)
        self._validate_invitation_restrictions()
        self._validate_series_pass_rules()

    def _validate_series_pass_rules(self) -> None:
        """A changed purchase rule must still admit every buyer of a series pass that covers this tier (#1341).

        Only when the rule changes, as ``SeriesPass.clean`` does on its side: a link that predates #1332 does not
        block unrelated edits. ``update_ticket_tier`` checks a new membership-tier restriction.
        """
        if self._state.adding:
            return
        stored = TicketTier.objects.filter(pk=self.pk).values_list("purchasable_by", flat=True).first()
        if stored is not None and stored != self.purchasable_by:
            self.assert_covers_series_passes()

    def assert_covers_series_passes(self, field: str = "purchasable_by") -> None:
        """Raise when a series pass that covers this tier admits a buyer whom the tier's rule refuses (#1341).

        Args:
            field: the error key, the input that made the rule stricter.

        It first locks the covering passes and then the tier row, in the global order, as ``add_tier_links`` does.
        So a new link, or a looser rule of a covering pass (``SeriesPass.clean`` locks the pass), and this change
        cannot both pass (#1618).
        """
        from .series_pass import SeriesPass  # series_pass imports this module

        lock_rows(SeriesPass, set(self.series_pass_links.values_list("series_pass_id", flat=True)))
        lock_row(TicketTier, self.pk)
        restricted = self.restricted_to_membership_tiers.exists()
        pass_rules = set(self.series_pass_links.values_list("series_pass__purchasable_by", flat=True))
        if any(
            tier_rule_stricter_than_pass(self.purchasable_by, rule, tier_restricted_to_membership_tiers=restricted)
            for rule in pass_rules
        ):
            raise DjangoValidationError(
                {
                    field: _(
                        "A series pass with a looser purchase rule covers this tier; change the pass or unlink the"
                        " tier first."
                    )
                }
            )

    def can_purchase(self) -> bool:
        """Check if the ticket can be purchased (inside the sales window and not paused)."""
        if self.sales_paused:
            return False
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
            models.Index(fields=["event", "display_order"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} for event {self.event.name}"


class Ticket(PreserveTicketNumberMixin, TimeStampedModel):
    """A ticket for a specific user to a specific event."""

    class TicketStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        CHECKED_IN = "checked_in", "Checked In"
        CANCELLED = "cancelled", "Cancelled"

    # Single definition of attendance: ACTIVE or CHECKED_IN. Use ``Ticket.objects.attending()``
    # where a queryset is available and this tuple in relation lookups (``tickets__status__in``).
    ATTENDING_STATUSES = (TicketStatus.ACTIVE, TicketStatus.CHECKED_IN)

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
        blank=True,
        help_text="Name of the ticket holder (may differ from purchasing user).",
    )
    # Snapshot, not input (as MembershipRecord.email): not an EmailField, so full_clean never rejects it.
    holder_email = models.CharField(max_length=254, blank=True, default="", db_default="", editable=False)
    price_paid = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Amount paid for THIS ticket, when tier.price alone cannot reconstruct it: "
        "PWYC, a discount code, or a tier that prices seats per category. "
        "Null for online payments (Payment.amount is authoritative there — it is net for a "
        "reverse-charge buyer, so stamping it here would put two different 'price paid' numbers "
        "on one row) and null for a plain purchase on a flat tier (use tier.price).",
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

    # Discount code tracking
    discount_code = models.ForeignKey(
        "events.DiscountCode",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Amount discounted from the original tier price.",
    )
    # RESTRICT (not PROTECT): a cascading user.delete() must still succeed — this
    # ticket is already being cascade-deleted via Ticket.user from the same origin,
    # which satisfies the restriction — while a direct held_pass.delete() (no wider
    # cascade in play) is still blocked, preserving the purchase/attendance audit trail.
    held_pass = models.ForeignKey(
        "events.HeldSeriesPass",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="tickets",
        help_text="Set when this ticket was materialized from a series pass purchase.",
    )

    refund_policy_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text="Copy of tier.refund_policy at purchase time. Immutable — drives refund math.",
    )
    attribution = models.JSONField(
        null=True,
        blank=True,
        help_text="Campaign tags (utm_source/medium/campaign/content) the buyer arrived with, "
        "as sent by the checkout payload (#922). Null when the URL carried none. Never updated.",
    )
    sale_source = models.CharField(
        max_length=20,
        choices=TicketSaleSource.choices,
        null=True,
        blank=True,
        editable=False,
        help_text="How the ticket was issued (#1013). Null for tickets created before this was recorded.",
    )
    unconfirmed_guest = models.BooleanField(
        default=False,
        db_default=False,
        editable=False,
        help_text="Issued to a guest account without email confirmation (paid guest checkout, #1430). "
        "Confers no standing (ADR-0020). Never updated.",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True, editable=False)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_tickets",
        editable=False,
    )
    cancellation_source = models.CharField(
        max_length=20,
        choices=CancellationSource.choices,
        blank=True,
        default="",
        db_index=True,
    )
    cancellation_reason = models.CharField(max_length=500, blank=True, default="")
    offline_refund_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        editable=False,
        help_text="Amount refunded for a manually-collected offline/at-the-door ticket. "
        "Set when a paid ticket is cancelled — 0.00 means the organizer kept the money (#1010); "
        "null means nothing was collected (or the ticket is not cancelled). "
        "Online (Stripe) refunds are tracked on Payment.refund_amount instead.",
    )

    # Fiscal ticket number (EU layer 1): per organization, assigned without gaps when the ticket is
    # first issued (ACTIVE/CHECKED_IN), never reused; holes only if numbered tickets are deleted (#1068).
    ticket_series = models.CharField(max_length=16, blank=True, default="", editable=False)
    ticket_number = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    issued_at = models.DateTimeField(null=True, blank=True, editable=False)

    # Cached ticket files (generated on-demand, cleaned up after event ends)
    pdf_file = ProtectedFileField(upload_to="tickets/pdf/", null=True, blank=True)
    pkpass_file = ProtectedFileField(upload_to="tickets/pkpass/", null=True, blank=True)
    file_content_hash = models.CharField(max_length=64, null=True, blank=True)

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
            models.UniqueConstraint(
                fields=["held_pass", "event"],
                condition=models.Q(held_pass__isnull=False) & ~models.Q(status="cancelled"),
                name="unique_held_pass_ticket_per_event",
            ),
        ]
        ordering = ["-created_at"]

    def _validate_seat(self) -> VenueSector | None:
        """Validate and auto-fill sector from seat. Returns the sector for chaining."""
        seat = self.seat
        sector = self.sector

        if seat:
            if not seat.is_active:
                raise DjangoValidationError({"seat": _("Cannot assign an inactive seat.")})

            if not self.sector_id:
                self.sector_id = seat.sector_id
                sector = seat.sector
            elif seat.sector_id != self.sector_id:
                raise DjangoValidationError({"seat": _("Seat must belong to the specified sector.")})

        return sector

    def _validate_sector(self, sector: VenueSector | None) -> None:
        """Validate and auto-fill venue from sector."""
        if sector:
            if not self.venue_id:
                self.venue_id = sector.venue_id
            elif sector.venue_id != self.venue_id:
                raise DjangoValidationError({"sector": _("Sector must belong to the specified venue.")})

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
        return apple_wallet_configured()

    @cached_property
    def google_pass_available(self) -> bool:
        """Check if a Google Wallet pass is available."""
        return google_wallet_configured()


def _get_payment_default_expiry() -> datetime:
    return timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)


# Payment now lives in .payment (file-length limit); re-exported here since most call sites
# still do `from events.models.ticket import Payment`.
from .payment import Payment as Payment  # noqa: E402, F401
