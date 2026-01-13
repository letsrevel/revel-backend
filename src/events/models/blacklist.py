import typing as t

from django.conf import settings
from django.db import models
from django.db.models import Q

from accounts.validators import normalize_phone_number
from common.models import TimeStampedModel

if t.TYPE_CHECKING:
    from .organization import Organization


class BlacklistQuerySet(models.QuerySet["Blacklist"]):
    """Custom QuerySet for Blacklist with filtering methods."""

    def for_organization(self, organization: "Organization") -> t.Self:
        """Filter by organization."""
        return self.filter(organization=organization)

    def linked(self) -> t.Self:
        """Return only entries linked to a user."""
        return self.filter(user__isnull=False)

    def unlinked(self) -> t.Self:
        """Return only entries not linked to a user (for fuzzy matching)."""
        return self.filter(user__isnull=True)

    def with_names(self) -> t.Self:
        """Return entries that have at least one name field populated."""
        return self.filter(Q(first_name__isnull=False) | Q(last_name__isnull=False) | Q(preferred_name__isnull=False))


class BlacklistManager(models.Manager["Blacklist"]):
    """Manager for Blacklist model."""

    def get_queryset(self) -> BlacklistQuerySet:
        """Return custom queryset."""
        return BlacklistQuerySet(self.model, using=self._db)

    def for_organization(self, organization: "Organization") -> BlacklistQuerySet:
        """Filter by organization."""
        return self.get_queryset().for_organization(organization)

    def linked(self) -> BlacklistQuerySet:
        """Return only entries linked to a user."""
        return self.get_queryset().linked()

    def unlinked(self) -> BlacklistQuerySet:
        """Return only entries not linked to a user."""
        return self.get_queryset().unlinked()


class Blacklist(TimeStampedModel):
    """Organization-scoped blacklist for blocking users.

    Supports three matching strategies:
    1. FK match - user is directly linked (most reliable)
    2. Hard match - email, telegram_username, or phone_number for unregistered imports
    3. Fuzzy match - name fields for soft blocking with verification flow
    """

    organization = models.ForeignKey(
        "events.Organization",
        on_delete=models.CASCADE,
        related_name="blacklist_entries",
    )

    # Confirmed match (populated on registration/telegram connect or quick blacklist)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="blacklist_entries",
    )

    # Hard identifiers (normalized, for matching unregistered users)
    email = models.EmailField(null=True, blank=True, db_index=True)
    telegram_username = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Telegram username without @ prefix",
    )
    phone_number = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        db_index=True,
        help_text="Phone number in E.164 format",
    )

    # Name fields for fuzzy matching (all optional)
    first_name = models.CharField(max_length=150, null=True, blank=True)
    last_name = models.CharField(max_length=150, null=True, blank=True)
    preferred_name = models.CharField(max_length=150, null=True, blank=True)

    reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )

    objects = BlacklistManager()

    class Meta:
        verbose_name = "Blacklist Entry"
        verbose_name_plural = "Blacklist Entries"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "user"], name="blacklist_org_user"),
            models.Index(fields=["organization", "email"], name="blacklist_org_email"),
        ]
        constraints = [
            # Unique user per org (when user is set)
            models.UniqueConstraint(
                fields=["organization", "user"],
                condition=Q(user__isnull=False),
                name="unique_blacklist_user_per_org",
            ),
            # Unique email per org (when email set, user null)
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=Q(email__isnull=False, user__isnull=True),
                name="unique_blacklist_email_per_org",
            ),
            # Unique telegram per org (when telegram set, user null)
            models.UniqueConstraint(
                fields=["organization", "telegram_username"],
                condition=Q(telegram_username__isnull=False, user__isnull=True),
                name="unique_blacklist_telegram_per_org",
            ),
            # Unique phone per org (when phone set, user null)
            models.UniqueConstraint(
                fields=["organization", "phone_number"],
                condition=Q(phone_number__isnull=False, user__isnull=True),
                name="unique_blacklist_phone_per_org",
            ),
        ]

    def __str__(self) -> str:
        if self.user:
            return f"Blacklist: {self.user} from {self.organization}"
        identifiers = []
        if self.email:
            identifiers.append(self.email)
        if self.telegram_username:
            identifiers.append(f"@{self.telegram_username}")
        if self.first_name or self.last_name:
            name = f"{self.first_name or ''} {self.last_name or ''}".strip()
            identifiers.append(name)
        return f"Blacklist: {', '.join(identifiers) or 'Unknown'} from {self.organization}"

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Normalize identifiers before saving."""
        if self.email:
            self.email = self.email.lower().strip()
        if self.telegram_username:
            self.telegram_username = self.telegram_username.lstrip("@").lower().strip()
        if self.phone_number:
            self.phone_number = normalize_phone_number(self.phone_number)
        super().save(*args, **kwargs)


class WhitelistRequest(TimeStampedModel):
    """Request to be whitelisted despite fuzzy-matching a blacklist entry.

    Similar to EventInvitationRequest - users can request verification when
    their name fuzzy-matches a blacklist entry.

    This model serves as both the request workflow AND the whitelist itself:
    - PENDING: User is awaiting admin review
    - APPROVED: User is whitelisted (can access despite fuzzy matches)
    - REJECTED: User is blocked

    Admins can delete an APPROVED request to "un-whitelist" a user,
    allowing them to submit a new request if needed.
    """

    class Status(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"

    organization = models.ForeignKey(
        "events.Organization",
        on_delete=models.CASCADE,
        related_name="whitelist_requests",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="whitelist_requests",
    )
    matched_blacklist_entries = models.ManyToManyField(
        Blacklist,
        related_name="triggered_whitelist_requests",
        help_text="The blacklist entries that triggered this request",
    )
    message = models.TextField(
        blank=True,
        help_text="User's explanation for why they should be whitelisted",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Whitelist Request"
        verbose_name_plural = "Whitelist Requests"
        ordering = ["-created_at"]
        constraints = [
            # Only one PENDING request per user/org at a time
            models.UniqueConstraint(
                fields=["organization", "user"],
                condition=Q(status="pending"),
                name="unique_pending_whitelist_request_per_org",
            ),
        ]

    def __str__(self) -> str:
        return f"Whitelist Request: {self.user} for {self.organization} ({self.status})"
