# User Ticket Cancellation & Refunds — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let ticket holders cancel their own tickets with automatic partial Stripe refunds driven by a per-tier, organizer-configured policy snapshotted at purchase time. Fix a pre-existing webhook bug that would cascade-cancel batch purchases.

**Architecture:** A new pure `cancellation_service` computes refund quotes from a `refund_policy_snapshot` stored on each `Ticket`. The ticket-holder-facing cancel endpoint executes Stripe refund + DB mutations in one transaction. The Stripe `charge.refunded` webhook is refactored to match refunds to specific `Payment` rows (instead of blanket-matching by `payment_intent_id`) so partial refunds in a batch no longer cascade.

**Tech Stack:** Django 5.2, Django Ninja (Extra), Pydantic v2, Stripe Python SDK, PostgreSQL, pytest.

**Reference:** [Design spec](../specs/2026-04-23-user-ticket-cancellation-design.md) — contains every architectural decision including multiple corrections to the original issue.

---

## Ground rules

- **Do not run tests.** The user runs tests. Plan steps that say "Run: pytest …" are for the user / the executing agent, not for asking the user to approve.
- **Never commit without asking.** At each commit step, show `git status` and the draft message first; wait for approval.
- **Follow CLAUDE.md conventions:** `import typing as t` (never `from typing import ...`); services return models; controllers are thin; `pydantic.AwareDatetime` for datetime fields in schemas; enum fields reference model enums directly in schemas; file length ≤ 1000 lines.
- **ModelSchema**: omit `created_at`/`updated_at` unless explicitly needed.
- **Schema re-exports:** every new class added to `events/schema/ticket.py` MUST also be added to `events/schema/__init__.py` (import + `__all__`).

---

## File structure

### New files

| Path | Responsibility |
|---|---|
| `src/events/utils/refund_policy.py` | `RefundPolicy` / `RefundPolicyTier` Pydantic schemas + `validate_refund_policy()` helper. Pure utilities; no DB, no imports from services. |
| `src/events/service/cancellation_service.py` | `quote_cancellation`, `build_cancellation_preview`, `cancel_ticket_by_user`. Pure quote + transactional execute. |
| `src/events/migrations/0068_ticket_cancellation_and_refunds.py` | Adds new fields on `TicketTier`, `Ticket`, `Payment`. |
| `src/events/tests/test_utils/test_refund_policy.py` | Unit tests for the Pydantic validator. |
| `src/events/tests/test_service/test_cancellation_service.py` | Unit tests for quote + preview + refund math. |
| `src/events/tests/test_controllers/test_event_public/test_ticket_cancellation.py` | Integration tests for the new ticket-holder endpoints. |
| `src/events/tests/test_controllers/test_stripe_webhook_refund_matching.py` | Integration tests for the refactored webhook matching strategy. |

### Modified files

| Path | Change |
|---|---|
| `src/events/models/ticket.py` | Add enums (`CancellationSource`, `CancellationBlockReason`, `Payment.RefundStatus`) + fields to `TicketTier`, `Ticket`, `Payment`. |
| `src/events/schema/ticket.py` | Add `RefundPolicy*`, `CancellationPreviewSchema`, `RefundWindow`, `TicketCancellationRequest`, `TicketCancellationResponse`, `AdminCancelTicketSchema`; extend `TicketTierCreate/UpdateSchema` with the three new fields. |
| `src/events/schema/__init__.py` | Re-export new schemas. |
| `src/events/service/batch_ticket_service.py` | Populate `Ticket.refund_policy_snapshot` at bulk create. |
| `src/events/service/stripe_webhooks.py` | Refactor `handle_charge_refunded` to per-payment matching (five branches). |
| `src/events/service/ticket_service.py` | Accept new tier fields in `create_ticket_tier` / `update_ticket_tier`. |
| `src/events/controllers/event_public/tickets.py` | Add `GET /tickets/{id}/cancellation-preview` + `POST /tickets/{id}/cancel`. |
| `src/events/controllers/event_admin/tickets.py` | Backfill audit fields on admin `cancel_ticket` & `mark_ticket_refunded`; capture `_original_ticket_status` in `mark_ticket_refunded`. Accept optional `cancellation_reason`. |
| `src/notifications/signals/payment.py` | Use `payment.refund_amount` (fallback to `payment.amount`) in `_send_refund_notifications`. |
| `src/notifications/signals/ticket.py` | Add `cancellation_source` + `cancellation_reason` to `_build_ticket_updated_context`. |
| `src/notifications/templates/notifications/{in_app,email,telegram}/ticket_cancelled.*` | Branch copy on `cancellation_source`; surface `cancellation_reason`. |

Each migration/model/service/endpoint change is a separately-committable task below.

---

## Phase 1 — RefundPolicy pydantic schema & validator (pure)

### Task 1.1: Write the failing test for `RefundPolicy` validation

**Files:**
- Create: `src/events/tests/test_utils/__init__.py` (empty)
- Create: `src/events/tests/test_utils/test_refund_policy.py`

- [ ] **Step 1: Create the tests/test_utils package marker**

Write empty file at `src/events/tests/test_utils/__init__.py` (zero bytes).

- [ ] **Step 2: Write the test file**

Create `src/events/tests/test_utils/test_refund_policy.py`:

```python
"""Unit tests for RefundPolicy Pydantic validation."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from events.utils.refund_policy import RefundPolicy, RefundPolicyTier, validate_refund_policy


class TestRefundPolicyTierValidation:
    def test_valid_tier(self) -> None:
        tier = RefundPolicyTier(hours_before_event=48, refund_percentage=Decimal("100"))
        assert tier.hours_before_event == 48
        assert tier.refund_percentage == Decimal("100")

    def test_hours_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            RefundPolicyTier(hours_before_event=-1, refund_percentage=Decimal("50"))

    def test_percentage_must_be_in_0_100(self) -> None:
        with pytest.raises(ValidationError):
            RefundPolicyTier(hours_before_event=1, refund_percentage=Decimal("101"))
        with pytest.raises(ValidationError):
            RefundPolicyTier(hours_before_event=1, refund_percentage=Decimal("-1"))


class TestRefundPolicyValidation:
    def test_single_tier_policy_is_valid(self) -> None:
        policy = RefundPolicy(
            tiers=[RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("100"))],
            flat_fee=Decimal("0"),
        )
        assert len(policy.tiers) == 1

    def test_multi_tier_strictly_descending_hours_is_valid(self) -> None:
        policy = RefundPolicy(
            tiers=[
                RefundPolicyTier(hours_before_event=168, refund_percentage=Decimal("100")),
                RefundPolicyTier(hours_before_event=48, refund_percentage=Decimal("50")),
                RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("25")),
            ]
        )
        assert len(policy.tiers) == 3

    def test_non_descending_hours_rejected(self) -> None:
        with pytest.raises(ValidationError, match="hours_before_event"):
            RefundPolicy(
                tiers=[
                    RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("100")),
                    RefundPolicyTier(hours_before_event=48, refund_percentage=Decimal("50")),
                ]
            )

    def test_equal_hours_rejected(self) -> None:
        with pytest.raises(ValidationError, match="hours_before_event"):
            RefundPolicy(
                tiers=[
                    RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("100")),
                    RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("50")),
                ]
            )

    def test_increasing_refund_percentage_rejected(self) -> None:
        with pytest.raises(ValidationError, match="refund_percentage"):
            RefundPolicy(
                tiers=[
                    RefundPolicyTier(hours_before_event=48, refund_percentage=Decimal("50")),
                    RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("100")),
                ]
            )

    def test_negative_flat_fee_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RefundPolicy(
                tiers=[RefundPolicyTier(hours_before_event=0, refund_percentage=Decimal("50"))],
                flat_fee=Decimal("-1"),
            )


class TestValidateRefundPolicyHelper:
    def test_none_returns_none(self) -> None:
        assert validate_refund_policy(None) is None

    def test_valid_dict_returns_policy(self) -> None:
        result = validate_refund_policy(
            {
                "tiers": [{"hours_before_event": 24, "refund_percentage": "100"}],
                "flat_fee": "0",
            }
        )
        assert isinstance(result, RefundPolicy)
        assert result.tiers[0].hours_before_event == 24

    def test_invalid_dict_raises(self) -> None:
        with pytest.raises(ValidationError):
            validate_refund_policy(
                {
                    "tiers": [
                        {"hours_before_event": 24, "refund_percentage": "50"},
                        {"hours_before_event": 48, "refund_percentage": "100"},
                    ],
                    "flat_fee": "0",
                }
            )
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest src/events/tests/test_utils/test_refund_policy.py -v`
Expected: ImportError / ModuleNotFoundError for `events.utils.refund_policy`.

### Task 1.2: Implement `RefundPolicy` + validator

**Files:**
- Create: `src/events/utils/refund_policy.py`

- [ ] **Step 1: Create the module**

Write `src/events/utils/refund_policy.py`:

```python
"""Pydantic schemas + helpers for the per-tier ticket refund policy.

This module is pure utility — no DB access, no imports from services.
Safe to import from models, admin, and service code alike.
"""

import typing as t
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


class RefundPolicyTier(BaseModel):
    """One bracket of the refund policy: refund X% if cancelling at least N hours before event."""

    hours_before_event: int = Field(ge=0)
    refund_percentage: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)


class RefundPolicy(BaseModel):
    """A tier's cancellation refund policy.

    ``tiers`` must be ordered by strictly-descending ``hours_before_event`` and
    monotonically non-increasing ``refund_percentage`` (a tier offering a higher
    refund percentage later in time than an earlier bracket is rejected).
    """

    tiers: list[RefundPolicyTier] = Field(min_length=1)
    flat_fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)

    @model_validator(mode="after")
    def _validate_monotonic(self) -> "RefundPolicy":
        for i in range(1, len(self.tiers)):
            prev = self.tiers[i - 1]
            curr = self.tiers[i]
            if curr.hours_before_event >= prev.hours_before_event:
                raise ValueError("hours_before_event must be strictly descending across tiers")
            if curr.refund_percentage > prev.refund_percentage:
                raise ValueError("refund_percentage must be monotonically non-increasing across tiers")
        return self


def validate_refund_policy(data: dict[str, t.Any] | None) -> RefundPolicy | None:
    """Parse & validate a stored/inbound refund policy dict.

    Args:
        data: Raw dict (e.g. from JSONField) or None.

    Returns:
        ``RefundPolicy`` instance, or ``None`` when ``data`` is ``None``.

    Raises:
        pydantic.ValidationError: if ``data`` is malformed.
    """
    if data is None:
        return None
    return RefundPolicy.model_validate(data)
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest src/events/tests/test_utils/test_refund_policy.py -v`
Expected: all pass.

- [ ] **Step 3: Commit (ask user first)**

```bash
git add src/events/utils/refund_policy.py src/events/tests/test_utils/
git status
```

Draft message:

```
feat(events): add RefundPolicy pydantic schema + validator

Pure utility module used by tier admin endpoints and the cancellation
service. Validates strictly descending hours_before_event and
monotonically non-increasing refund_percentage.
```

---

## Phase 2 — Data model & migration

### Task 2.1: Add enums + fields to `TicketTier`, `Ticket`, `Payment`

**Files:**
- Modify: `src/events/models/ticket.py`

- [ ] **Step 1: Add `CancellationSource` and `CancellationBlockReason` enums near the top of the file**

Insert after the imports and the `DEFAULT_TICKET_TIER_NAME` constant, around line 26:

```python
class CancellationSource(models.TextChoices):
    """Who or what cancelled the ticket."""

    USER = "user", "User self-service"
    ORGANIZER = "organizer", "Organizer via admin"
    STRIPE_DASHBOARD = "stripe_dashboard", "Stripe dashboard"


class CancellationBlockReason(models.TextChoices):
    """Stable error codes surfaced to the frontend when cancellation is blocked.

    These values are i18n keys — the frontend owns the localized copy.
    """

    ALREADY_CANCELLED = "already_cancelled", "Ticket is already cancelled"
    CHECKED_IN = "checked_in", "Ticket holder has already checked in"
    EVENT_STARTED = "event_started", "Event has already started"
    NOT_PERMITTED = "not_permitted", "Self-service cancellation disabled for this tier"
    PAST_DEADLINE = "past_deadline", "Past the cancellation deadline"
    NOT_OWNER = "not_owner", "Only the ticket holder can cancel this ticket"
```

- [ ] **Step 2: Add new `TicketTier` fields**

Inside `class TicketTier(...)`, right before `restrict_visibility_to_linked_invitations` (around line 326), add:

```python
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
```

- [ ] **Step 3: Add new `Ticket` fields**

Inside `class Ticket(...)`, right before `file_content_hash` (around line 638), add:

```python
    refund_policy_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text="Copy of tier.refund_policy at purchase time. Immutable — drives refund math.",
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
```

- [ ] **Step 4: Add `Payment.RefundStatus` enum + refund fields**

Inside `class Payment(...)` below `PaymentStatus` (around line 712), add:

```python
    class RefundStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
```

Then below `buyer_billing_snapshot` and before `raw_response` (around line 760), add:

```python
    refund_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Actual amount refunded. May be less than amount when a partial refund policy applies.",
    )
    refund_status = models.CharField(
        max_length=20,
        choices=RefundStatus.choices,
        null=True,
        blank=True,
        db_index=True,
    )
    stripe_refund_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
        help_text="Stripe refund object id. Used to match webhook refund events to this Payment.",
    )
    refund_failure_reason = models.TextField(blank=True, default="")
    refunded_at = models.DateTimeField(null=True, blank=True)
```

### Task 2.2: Generate and commit the migration

- [ ] **Step 1: Generate migration**

Run: `make migrations`

Expected output: a new file `src/events/migrations/0068_*.py` is created.

- [ ] **Step 2: Rename the migration**

Rename the generated file to `0068_ticket_cancellation_and_refunds.py` (preserve its contents). If Django picked a different number, rename so `0068_` prefix matches the spec's numbering.

- [ ] **Step 3: Run migration checks**

Run: `make check`
Expected: migration-check passes; mypy passes; no lint errors.

- [ ] **Step 4: Commit (ask user first)**

```bash
git add src/events/models/ticket.py src/events/migrations/0068_ticket_cancellation_and_refunds.py
git status
```

Draft message:

```
feat(events): data model for ticket cancellation & refunds (#370)

Adds TicketTier.allow_user_cancellation / cancellation_deadline_hours /
refund_policy; Ticket.refund_policy_snapshot + audit fields; Payment
refund lifecycle fields + RefundStatus enum. CancellationSource and
CancellationBlockReason enums live on events.models.ticket for reuse
across the stack. No behavior change — all defaults preserve existing
semantics.
```

---

## Phase 3 — Cancellation quote & preview (pure service)

### Task 3.1: Write failing unit tests for `quote_cancellation`

**Files:**
- Create: `src/events/tests/test_service/test_cancellation_service.py`

- [ ] **Step 1: Write the test file**

Create `src/events/tests/test_service/test_cancellation_service.py`:

```python
"""Unit tests for cancellation_service.quote_cancellation and build_cancellation_preview."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from events.models import Ticket, TicketTier
from events.models.ticket import CancellationBlockReason
from events.service.cancellation_service import build_cancellation_preview, quote_cancellation

pytestmark = pytest.mark.django_db


def _policy(*tiers: tuple[int, str], flat_fee: str = "0") -> dict:
    return {
        "tiers": [
            {"hours_before_event": hours, "refund_percentage": pct} for hours, pct in tiers
        ],
        "flat_fee": flat_fee,
    }


class TestQuoteCancellationBlockReasons:
    def test_already_cancelled(self, ticket_factory, tier_online_with_cancellation_enabled):
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled, status=Ticket.TicketStatus.CANCELLED)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.ALREADY_CANCELLED
        assert result.refund_amount == Decimal("0")

    def test_checked_in(self, ticket_factory, tier_online_with_cancellation_enabled):
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled, status=Ticket.TicketStatus.CHECKED_IN)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.CHECKED_IN

    def test_event_started(self, ticket_factory, tier_online_with_cancellation_enabled, event):
        event.start = timezone.now() - timedelta(minutes=1)
        event.save(update_fields=["start"])
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.EVENT_STARTED

    def test_tier_not_permitting_cancellation(self, ticket_factory, tier_online_with_cancellation_disabled):
        ticket = ticket_factory(tier=tier_online_with_cancellation_disabled)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.NOT_PERMITTED

    def test_past_deadline(self, ticket_factory, tier_factory, event):
        # Event starts in 10 hours, deadline is 24 hours before → we're past it.
        event.start = timezone.now() + timedelta(hours=10)
        event.save(update_fields=["start"])
        tier = tier_factory(
            allow_user_cancellation=True,
            cancellation_deadline_hours=24,
            refund_policy=_policy((0, "100")),
        )
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=tier.refund_policy)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.PAST_DEADLINE


class TestQuoteCancellationRefundMath:
    def test_no_snapshot_means_zero_refund_but_can_cancel(self, ticket_factory, tier_online_with_cancellation_enabled):
        # Snapshot = None → refund 0, cancellation still permitted.
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled, refund_policy_snapshot=None)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is True
        assert result.refund_amount == Decimal("0")

    def test_first_tier_branch(self, ticket_factory, tier_factory, event, payment_factory):
        # 200 hours before event, tier at 168h=100% applies.
        event.start = timezone.now() + timedelta(hours=200)
        event.save(update_fields=["start"])
        policy = _policy((168, "100"), (48, "50"), (24, "25"))
        tier = tier_factory(allow_user_cancellation=True, refund_policy=policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is True
        assert result.refund_amount == Decimal("40.00")

    def test_middle_tier_branch(self, ticket_factory, tier_factory, event, payment_factory):
        event.start = timezone.now() + timedelta(hours=72)
        event.save(update_fields=["start"])
        policy = _policy((168, "100"), (48, "50"), (24, "25"))
        tier = tier_factory(allow_user_cancellation=True, refund_policy=policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        result = quote_cancellation(ticket, timezone.now())
        assert result.refund_amount == Decimal("20.00")

    def test_no_tier_matches_yields_zero_refund_still_cancellable(
        self, ticket_factory, tier_factory, event, payment_factory
    ):
        event.start = timezone.now() + timedelta(hours=10)  # past all tiers
        event.save(update_fields=["start"])
        policy = _policy((48, "50"), (24, "25"))
        tier = tier_factory(allow_user_cancellation=True, refund_policy=policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is True
        assert result.refund_amount == Decimal("0")

    def test_flat_fee_floored_at_zero(self, ticket_factory, tier_factory, event, payment_factory):
        event.start = timezone.now() + timedelta(hours=72)
        event.save(update_fields=["start"])
        policy = _policy((48, "50"), flat_fee="100")  # base_refund < flat_fee
        tier = tier_factory(allow_user_cancellation=True, refund_policy=policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        result = quote_cancellation(ticket, timezone.now())
        assert result.refund_amount == Decimal("0")

    def test_offline_tier_always_zero_refund(self, ticket_factory, tier_factory, event):
        event.start = timezone.now() + timedelta(hours=72)
        event.save(update_fields=["start"])
        policy = _policy((48, "100"))
        tier = tier_factory(
            allow_user_cancellation=True,
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            refund_policy=policy,
        )
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is True
        assert result.refund_amount == Decimal("0")

    def test_snapshot_authority_ignores_mutated_tier_policy(
        self, ticket_factory, tier_factory, event, payment_factory
    ):
        event.start = timezone.now() + timedelta(hours=72)
        event.save(update_fields=["start"])
        frozen_policy = _policy((48, "100"))
        tier = tier_factory(allow_user_cancellation=True, refund_policy=frozen_policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=frozen_policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        # Mutate the tier policy post-purchase to something worse.
        tier.refund_policy = _policy((48, "0"))
        tier.save(update_fields=["refund_policy"])
        result = quote_cancellation(ticket, timezone.now())
        assert result.refund_amount == Decimal("40.00")


class TestBuildCancellationPreview:
    def test_three_tier_policy_produces_three_windows(
        self, ticket_factory, tier_factory, event, payment_factory
    ):
        event.start = timezone.now() + timedelta(hours=300)
        event.save(update_fields=["start"])
        policy = _policy((168, "100"), (48, "50"), (24, "25"), flat_fee="1")
        tier = tier_factory(allow_user_cancellation=True, refund_policy=policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        preview = build_cancellation_preview(ticket, timezone.now())
        assert preview.can_cancel is True
        assert [w.refund_percentage for w in preview.windows] == [Decimal("100"), Decimal("50"), Decimal("25")]
        # Flat fee subtracted on each window
        assert preview.windows[0].refund_amount == Decimal("39.00")
        assert preview.windows[1].refund_amount == Decimal("19.00")
        assert preview.windows[2].refund_amount == Decimal("9.00")

    def test_empty_policy_yields_empty_windows(self, ticket_factory, tier_online_with_cancellation_enabled):
        ticket = ticket_factory(
            tier=tier_online_with_cancellation_enabled,
            refund_policy_snapshot=None,
        )
        preview = build_cancellation_preview(ticket, timezone.now())
        assert preview.windows == []
```

- [ ] **Step 2: Add the required fixtures**

Append to `src/events/tests/test_service/conftest.py` (create if missing). The exact factory signatures depend on what already exists — check `src/events/tests/conftest.py` and `src/events/tests/test_service/conftest.py` first and **reuse** existing factories when possible. Add only what's missing:

```python
# -- Append to src/events/tests/test_service/conftest.py --
from decimal import Decimal

import pytest

from events.models import TicketTier


@pytest.fixture
def tier_online_with_cancellation_enabled(tier_factory):
    return tier_factory(
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price=Decimal("40.00"),
        allow_user_cancellation=True,
        refund_policy={
            "tiers": [{"hours_before_event": 48, "refund_percentage": "100"}],
            "flat_fee": "0",
        },
    )


@pytest.fixture
def tier_online_with_cancellation_disabled(tier_factory):
    return tier_factory(
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price=Decimal("40.00"),
        allow_user_cancellation=False,
    )
```

If `tier_factory`, `ticket_factory`, `payment_factory`, `event` are not already provided by `src/events/tests/conftest.py`, add thin factory fixtures there that build these objects with the ORM (mirror how other service tests build them — see `test_batch_ticket_service/conftest.py` for the pattern). Keep the factories minimal; any field accepted as `**kwargs` should override the default.

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest src/events/tests/test_service/test_cancellation_service.py -v`
Expected: `ImportError: cannot import name 'quote_cancellation' from 'events.service.cancellation_service'`.

### Task 3.2: Implement `quote_cancellation` + `build_cancellation_preview`

**Files:**
- Create: `src/events/service/cancellation_service.py`

- [ ] **Step 1: Write the service module**

Create `src/events/service/cancellation_service.py`:

```python
"""User-initiated ticket cancellation & refund quoting.

Two pure functions (``quote_cancellation``, ``build_cancellation_preview``)
compute refund amounts from a ticket's snapshot; ``cancel_ticket_by_user``
orchestrates the end-to-end flow including Stripe refund and DB mutations.
"""

import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import structlog
from pydantic import ValidationError as PydanticValidationError

from events.models import Ticket, TicketTier
from events.models.ticket import CancellationBlockReason
from events.utils.refund_policy import RefundPolicy, validate_refund_policy

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class RefundWindowDto:
    """One segment of the refund-vs-time curve for UI rendering."""

    refund_percentage: Decimal
    refund_amount: Decimal
    effective_until: datetime


@dataclass(frozen=True)
class RefundQuote:
    """Result of ``quote_cancellation``."""

    can_cancel: bool
    reason: CancellationBlockReason | None
    refund_amount: Decimal
    currency: str
    deadline: datetime | None


@dataclass(frozen=True)
class CancellationPreview:
    """Full preview payload for the preview endpoint."""

    can_cancel: bool
    reason: CancellationBlockReason | None
    refund_amount: Decimal
    currency: str
    deadline: datetime | None
    flat_fee: Decimal
    payment_method: str
    windows: list[RefundWindowDto] = field(default_factory=list)
    policy_snapshot: RefundPolicy | None = None


def _ticket_currency(ticket: Ticket) -> str:
    payment = getattr(ticket, "payment", None)
    if payment is not None:
        return str(payment.currency)
    return str(ticket.tier.currency)


def _ticket_amount(ticket: Ticket) -> Decimal:
    """Per-ticket refundable gross amount. Online → payment.amount; otherwise 0."""
    payment = getattr(ticket, "payment", None)
    if payment is None:
        return _ZERO
    if ticket.tier.payment_method != TicketTier.PaymentMethod.ONLINE:
        return _ZERO
    return Decimal(payment.amount)


def _deadline(ticket: Ticket) -> datetime:
    hours = ticket.tier.cancellation_deadline_hours
    if hours is None:
        return ticket.event.start
    return ticket.event.start - timedelta(hours=hours)


def _load_snapshot(ticket: Ticket) -> RefundPolicy | None:
    """Load the snapshot, tolerating malformed historical data.

    Malformed stored data falls back to "no refund" rather than 500ing the preview.
    Logged for ops visibility.
    """
    try:
        return validate_refund_policy(ticket.refund_policy_snapshot)
    except PydanticValidationError as exc:
        logger.warning(
            "refund_policy_snapshot_invalid",
            ticket_id=str(ticket.id),
            error=str(exc),
        )
        return None


def _block_reason(ticket: Ticket, now: datetime) -> CancellationBlockReason | None:
    if ticket.status == Ticket.TicketStatus.CANCELLED:
        return CancellationBlockReason.ALREADY_CANCELLED
    if ticket.status == Ticket.TicketStatus.CHECKED_IN:
        return CancellationBlockReason.CHECKED_IN
    if now >= ticket.event.start:
        return CancellationBlockReason.EVENT_STARTED
    if not ticket.tier.allow_user_cancellation:
        return CancellationBlockReason.NOT_PERMITTED
    if now > _deadline(ticket):
        return CancellationBlockReason.PAST_DEADLINE
    return None


def _compute_refund(policy: RefundPolicy, hours_remaining: Decimal, gross: Decimal) -> Decimal:
    for tier in policy.tiers:
        if hours_remaining >= tier.hours_before_event:
            base = (gross * tier.refund_percentage) / Decimal(100)
            adjusted = base - policy.flat_fee
            if adjusted <= _ZERO:
                return _ZERO
            return adjusted.quantize(_CENT, rounding=ROUND_HALF_EVEN)
    return _ZERO


def quote_cancellation(ticket: Ticket, now: datetime) -> RefundQuote:
    """Compute whether ``ticket`` can be cancelled and the refund amount if so.

    Pure + stateless. Ownership (NOT_OWNER) is enforced by the controller, not here.
    """
    reason = _block_reason(ticket, now)
    currency = _ticket_currency(ticket)
    deadline = _deadline(ticket)

    if reason is not None:
        return RefundQuote(
            can_cancel=False,
            reason=reason,
            refund_amount=_ZERO,
            currency=currency,
            deadline=deadline,
        )

    gross = _ticket_amount(ticket)
    policy = _load_snapshot(ticket)
    if policy is None or gross == _ZERO:
        return RefundQuote(
            can_cancel=True,
            reason=None,
            refund_amount=_ZERO,
            currency=currency,
            deadline=deadline,
        )

    hours_remaining = Decimal((ticket.event.start - now).total_seconds()) / Decimal(3600)
    refund = _compute_refund(policy, hours_remaining, gross)
    return RefundQuote(
        can_cancel=True,
        reason=None,
        refund_amount=refund,
        currency=currency,
        deadline=deadline,
    )


def _derive_windows(
    policy: RefundPolicy,
    event_start: datetime,
    gross: Decimal,
) -> list[RefundWindowDto]:
    windows: list[RefundWindowDto] = []
    for tier in policy.tiers:
        base = (gross * tier.refund_percentage) / Decimal(100)
        adjusted = base - policy.flat_fee
        amount = (
            _ZERO
            if adjusted <= _ZERO
            else adjusted.quantize(_CENT, rounding=ROUND_HALF_EVEN)
        )
        windows.append(
            RefundWindowDto(
                refund_percentage=tier.refund_percentage,
                refund_amount=amount,
                effective_until=event_start - timedelta(hours=tier.hours_before_event),
            )
        )
    return windows


def build_cancellation_preview(ticket: Ticket, now: datetime) -> CancellationPreview:
    """Build the full preview DTO powering the UI timeline + decision state."""
    quote = quote_cancellation(ticket, now)
    policy = _load_snapshot(ticket)
    gross = _ticket_amount(ticket)
    windows = (
        _derive_windows(policy, ticket.event.start, gross)
        if policy is not None and gross > _ZERO
        else []
    )
    flat_fee = policy.flat_fee if policy is not None else _ZERO
    return CancellationPreview(
        can_cancel=quote.can_cancel,
        reason=quote.reason,
        refund_amount=quote.refund_amount,
        currency=quote.currency,
        deadline=quote.deadline,
        flat_fee=flat_fee,
        payment_method=ticket.tier.payment_method,
        windows=windows,
        policy_snapshot=policy,
    )
```

- [ ] **Step 2: Add `cancel_ticket_by_user` stub that raises `NotImplementedError`**

At the bottom of `cancellation_service.py`, add:

```python
def cancel_ticket_by_user(
    ticket: Ticket,
    user: t.Any,
    reason: str,
    now: datetime,
) -> t.Any:
    """End-to-end user-initiated cancellation. Implemented in Phase 5."""
    raise NotImplementedError
```

This keeps the file self-contained during Phase 3/4 tests without forcing the function's final shape yet.

- [ ] **Step 3: Run tests**

Run: `uv run pytest src/events/tests/test_service/test_cancellation_service.py -v`
Expected: all pass.

- [ ] **Step 4: Commit (ask user first)**

```bash
git add src/events/service/cancellation_service.py src/events/tests/test_service/test_cancellation_service.py src/events/tests/test_service/conftest.py
git status
```

Draft message:

```
feat(events): pure refund-quote and preview service (#370)

Stateless quote_cancellation + build_cancellation_preview operating on
ticket.refund_policy_snapshot. Ownership (NOT_OWNER) is enforced in the
controller, not here. cancel_ticket_by_user stubbed for Phase 5.
```

---

## Phase 4 — Populate `refund_policy_snapshot` at ticket creation

### Task 4.1: Write a failing test asserting snapshot is copied on bulk create

**Files:**
- Create: `src/events/tests/test_service/test_batch_ticket_service/test_refund_snapshot.py` (place it next to the existing batch ticket service tests).

- [ ] **Step 1: Inspect the existing batch service test layout**

Look at `src/events/tests/test_service/test_batch_ticket_service/` so the new test fits the established conftest & fixtures. Reuse the existing online/offline/free fixtures.

- [ ] **Step 2: Write the test**

```python
"""Tickets created via BatchTicketService snapshot the tier's refund_policy."""

from decimal import Decimal

import pytest

from events.models import Ticket, TicketTier

pytestmark = pytest.mark.django_db


def test_offline_batch_purchase_snapshots_refund_policy(batch_offline_tier, batch_user, run_checkout_offline):
    batch_offline_tier.allow_user_cancellation = True
    batch_offline_tier.refund_policy = {
        "tiers": [{"hours_before_event": 24, "refund_percentage": "100"}],
        "flat_fee": "0",
    }
    batch_offline_tier.save(update_fields=["allow_user_cancellation", "refund_policy"])
    tickets = run_checkout_offline(count=2)
    assert len(tickets) == 2
    for ticket in tickets:
        assert ticket.refund_policy_snapshot == batch_offline_tier.refund_policy


def test_null_policy_snapshots_as_none(batch_offline_tier, batch_user, run_checkout_offline):
    assert batch_offline_tier.refund_policy is None
    tickets = run_checkout_offline(count=1)
    assert tickets[0].refund_policy_snapshot is None
```

If `run_checkout_offline` fixture doesn't exist yet, thin it in the surrounding `conftest.py` to drive the existing `BatchTicketService.create_batch(...)` for an offline tier with `count` items.

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest src/events/tests/test_service/test_batch_ticket_service/test_refund_snapshot.py -v`
Expected: FAIL — `ticket.refund_policy_snapshot` is None because `_create_tickets` doesn't set it yet.

### Task 4.2: Implement snapshot copy in `_create_tickets`

**Files:**
- Modify: `src/events/service/batch_ticket_service.py:514-541`

- [ ] **Step 1: Pass the snapshot during ticket construction**

Inside `_create_tickets`, where the loop builds `Ticket(...)` objects, add `refund_policy_snapshot=self.tier.refund_policy` to the kwargs. Example:

```python
ticket = Ticket(
    event=self.event,
    tier=self.tier,
    user=self.user,
    status=status,
    guest_name=item.guest_name,
    price_paid=price_paid,
    discount_code=dc,
    discount_amount=discount_amount,
    refund_policy_snapshot=self.tier.refund_policy,
)
```

Also extend the `ticket.clean_fields(exclude=[...])` call to include `"refund_policy_snapshot"` so that a `None` JSON value doesn't trip field validation.

- [ ] **Step 2: Run the snapshot test**

Run: `uv run pytest src/events/tests/test_service/test_batch_ticket_service/test_refund_snapshot.py -v`
Expected: pass.

- [ ] **Step 3: Run the full batch-service test suite to catch regressions**

Run: `uv run pytest src/events/tests/test_service/test_batch_ticket_service -v`
Expected: all pass.

- [ ] **Step 4: Commit (ask user first)**

```bash
git add src/events/service/batch_ticket_service.py src/events/tests/test_service/test_batch_ticket_service/test_refund_snapshot.py
git status
```

Draft message:

```
feat(events): snapshot tier.refund_policy onto new tickets (#370)

Each ticket stores its own immutable copy of tier.refund_policy at
purchase time. A later policy edit by the organizer does not
retroactively change refund amounts.
```

---

## Phase 5 — End-to-end user cancellation service

### Task 5.1: Implement `cancel_ticket_by_user`

**Files:**
- Modify: `src/events/service/cancellation_service.py`

The controller will translate exceptions raised here into HTTP responses. Use a small result DTO + two custom exception types so the controller never second-guesses the service's decision.

- [ ] **Step 1: Add exception types + result DTO near the top of the file**

Insert just above `RefundWindowDto`:

```python
class CancellationNotOwner(Exception):
    """Raised when the caller is not the ticket holder."""


class CancellationBlocked(Exception):
    """Raised when a business rule (ALREADY_CANCELLED, CHECKED_IN, ...) blocks cancellation."""

    def __init__(self, reason: CancellationBlockReason):
        super().__init__(str(reason.label))
        self.reason = reason


class StripeRefundFailed(Exception):
    """Raised when a Stripe refund attempt fails after all internal retries."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class CancellationResult:
    ticket: Ticket
    refund_amount: Decimal
    currency: str
    refund_status: str | None  # Payment.RefundStatus value, or None for offline/free
```

- [ ] **Step 2: Replace the stub `cancel_ticket_by_user` with the real implementation**

```python
def cancel_ticket_by_user(
    ticket: Ticket,
    user: t.Any,
    reason: str,
    now: datetime,
) -> CancellationResult:
    """Run the full user-initiated cancellation flow atomically.

    Raises:
        CancellationNotOwner: caller is not the ticket holder.
        CancellationBlocked: business-rule guard failed.
        StripeRefundFailed: Stripe refund API call failed.
    """
    from django.db import transaction
    from django.db.models import F

    from events.models import Payment, TicketTier as _TicketTier  # local import, avoid circular

    if ticket.user_id != user.id:
        raise CancellationNotOwner()

    quote = quote_cancellation(ticket, now)
    if not quote.can_cancel:
        # reason is guaranteed non-None when can_cancel is False
        assert quote.reason is not None
        raise CancellationBlocked(quote.reason)

    refund_status: str | None = None

    with transaction.atomic():
        # Lock the tier row to serialize inventory updates with concurrent purchases.
        _TicketTier.objects.select_for_update().filter(pk=ticket.tier_id).first()

        # Online tickets with refund > 0 → hit Stripe before mutating local state.
        payment: Payment | None = Payment.objects.filter(ticket=ticket).first()
        if (
            ticket.tier.payment_method == _TicketTier.PaymentMethod.ONLINE
            and payment is not None
            and payment.stripe_payment_intent_id
            and quote.refund_amount > _ZERO
        ):
            refund_status = _issue_stripe_refund(ticket, payment, quote.refund_amount)

        # Set pre-save hints used by the existing notification signal handlers.
        ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
        ticket._refund_amount = f"{quote.refund_amount} {quote.currency}"  # type: ignore[attr-defined]

        ticket.status = Ticket.TicketStatus.CANCELLED
        ticket.cancelled_at = now
        ticket.cancelled_by = user
        ticket.cancellation_source = "user"
        ticket.cancellation_reason = reason or ""
        ticket.save(
            update_fields=[
                "status",
                "cancelled_at",
                "cancelled_by",
                "cancellation_source",
                "cancellation_reason",
            ]
        )

        _TicketTier.objects.filter(pk=ticket.tier_id, quantity_sold__gt=0).update(
            quantity_sold=F("quantity_sold") - 1
        )

    return CancellationResult(
        ticket=ticket,
        refund_amount=quote.refund_amount,
        currency=quote.currency,
        refund_status=refund_status,
    )


def _issue_stripe_refund(ticket: Ticket, payment: t.Any, amount: Decimal) -> str:
    """Create a Stripe refund and mutate the Payment row. Returns refund_status.

    Raises ``StripeRefundFailed`` on any Stripe error so the enclosing atomic()
    rolls back and the ticket stays ACTIVE.
    """
    import stripe

    from events.models import Payment

    try:
        refund = stripe.Refund.create(
            payment_intent=payment.stripe_payment_intent_id,
            amount=int(amount * 100),
            metadata={"ticket_id": str(ticket.id), "user_initiated": "true"},
            idempotency_key=f"refund:{ticket.id}",
        )
    except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
        logger.error(
            "stripe_refund_failed",
            ticket_id=str(ticket.id),
            payment_id=str(payment.id),
            error=str(exc),
        )
        raise StripeRefundFailed(str(exc)) from exc

    payment.stripe_refund_id = refund.id
    payment.refund_amount = amount
    payment.refund_status = Payment.RefundStatus.PENDING
    payment.refund_failure_reason = ""
    payment.save(
        update_fields=[
            "stripe_refund_id",
            "refund_amount",
            "refund_status",
            "refund_failure_reason",
        ]
    )
    return str(Payment.RefundStatus.PENDING)
```

- [ ] **Step 3: Write integration tests for `cancel_ticket_by_user`**

Add to `src/events/tests/test_service/test_cancellation_service.py`:

```python
from unittest.mock import patch

from django.db.models import F

from events.models import Payment
from events.service.cancellation_service import (
    CancellationBlocked,
    CancellationNotOwner,
    StripeRefundFailed,
    cancel_ticket_by_user,
)


class TestCancelTicketByUser:
    def test_wrong_user_raises_not_owner(self, ticket_factory, user_factory, tier_online_with_cancellation_enabled):
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled)
        other = user_factory()
        with pytest.raises(CancellationNotOwner):
            cancel_ticket_by_user(ticket, other, reason="", now=timezone.now())

    def test_block_reason_raises_cancellation_blocked(self, ticket_factory, tier_online_with_cancellation_enabled):
        ticket = ticket_factory(
            tier=tier_online_with_cancellation_enabled, status=Ticket.TicketStatus.CANCELLED
        )
        with pytest.raises(CancellationBlocked) as info:
            cancel_ticket_by_user(ticket, ticket.user, reason="", now=timezone.now())
        assert info.value.reason == CancellationBlockReason.ALREADY_CANCELLED

    def test_free_ticket_cancels_no_stripe_call(self, ticket_factory, tier_factory, event):
        event.start = timezone.now() + timedelta(hours=72)
        event.save(update_fields=["start"])
        tier = tier_factory(
            payment_method=TicketTier.PaymentMethod.FREE,
            price=Decimal("0"),
            allow_user_cancellation=True,
        )
        ticket = ticket_factory(tier=tier)
        initial_sold = tier.quantity_sold
        tier.quantity_sold = 1
        tier.save(update_fields=["quantity_sold"])
        with patch("stripe.Refund.create") as mock_create:
            result = cancel_ticket_by_user(ticket, ticket.user, reason="moved", now=timezone.now())
        assert mock_create.call_count == 0
        assert result.refund_amount == Decimal("0")
        assert result.refund_status is None
        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.CANCELLED
        assert ticket.cancelled_by_id == ticket.user_id
        assert ticket.cancellation_source == "user"
        assert ticket.cancellation_reason == "moved"
        tier.refresh_from_db()
        assert tier.quantity_sold == 0

    def test_online_ticket_calls_stripe_with_idempotency_key(
        self, ticket_factory, tier_factory, event, payment_factory
    ):
        event.start = timezone.now() + timedelta(hours=72)
        event.save(update_fields=["start"])
        policy = {"tiers": [{"hours_before_event": 48, "refund_percentage": "100"}], "flat_fee": "0"}
        tier = tier_factory(
            payment_method=TicketTier.PaymentMethod.ONLINE,
            price=Decimal("40.00"),
            allow_user_cancellation=True,
            refund_policy=policy,
        )
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment = payment_factory(
            ticket=ticket, amount=Decimal("40.00"), stripe_payment_intent_id="pi_123"
        )
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_abc"
            result = cancel_ticket_by_user(ticket, ticket.user, reason="", now=timezone.now())
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        assert kwargs["payment_intent"] == "pi_123"
        assert kwargs["amount"] == 4000
        assert kwargs["idempotency_key"] == f"refund:{ticket.id}"
        assert kwargs["metadata"] == {"ticket_id": str(ticket.id), "user_initiated": "true"}
        assert result.refund_status == Payment.RefundStatus.PENDING
        payment.refresh_from_db()
        assert payment.stripe_refund_id == "re_abc"
        assert payment.refund_amount == Decimal("40.00")
        assert payment.refund_status == Payment.RefundStatus.PENDING

    def test_stripe_failure_rolls_back_transaction(
        self, ticket_factory, tier_factory, event, payment_factory
    ):
        import stripe
        event.start = timezone.now() + timedelta(hours=72)
        event.save(update_fields=["start"])
        policy = {"tiers": [{"hours_before_event": 48, "refund_percentage": "100"}], "flat_fee": "0"}
        tier = tier_factory(
            payment_method=TicketTier.PaymentMethod.ONLINE,
            price=Decimal("40.00"),
            allow_user_cancellation=True,
            refund_policy=policy,
        )
        tier.quantity_sold = 1
        tier.save(update_fields=["quantity_sold"])
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment = payment_factory(
            ticket=ticket, amount=Decimal("40.00"), stripe_payment_intent_id="pi_err"
        )

        with patch("stripe.Refund.create", side_effect=stripe.error.APIError("boom")):
            with pytest.raises(StripeRefundFailed):
                cancel_ticket_by_user(ticket, ticket.user, reason="", now=timezone.now())

        ticket.refresh_from_db()
        payment.refresh_from_db()
        tier.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.ACTIVE
        assert payment.refund_status is None
        assert tier.quantity_sold == 1
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest src/events/tests/test_service/test_cancellation_service.py -v`
Expected: all pass.

- [ ] **Step 5: Commit (ask user first)**

```bash
git add src/events/service/cancellation_service.py src/events/tests/test_service/test_cancellation_service.py
git status
```

Draft message:

```
feat(events): cancel_ticket_by_user service (#370)

Atomic Stripe refund + DB mutations, rollback on Stripe failure.
Custom exceptions (CancellationNotOwner, CancellationBlocked,
StripeRefundFailed) so the controller can map directly to HTTP codes.
```

---

## Phase 6 — Webhook refactor: match refunds per-Payment

The existing `handle_charge_refunded` is broken for batch partial refunds — it cascades. Fix before shipping the user-facing cancel, because the new flow would trip this bug immediately.

### Task 6.1: Write failing tests for the five matching branches

**Files:**
- Create: `src/events/tests/test_controllers/test_stripe_webhook_refund_matching.py`

- [ ] **Step 1: Inspect existing webhook tests**

Open `src/events/tests/test_controllers/test_stripe_webhook.py` for conventions — how it builds fake Stripe events and asserts DB side effects. Reuse its fixtures (it likely has one for "fake Stripe event" / the `StripeEventHandler`).

- [ ] **Step 2: Write the new test file**

```python
"""Tests for handle_charge_refunded's per-payment matching strategy."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import stripe

from events.models import Payment, Ticket
from events.service.stripe_webhooks import StripeEventHandler

pytestmark = pytest.mark.django_db


def _charge_event(payment_intent_id: str, refunds: list[dict]) -> stripe.Event:
    ev = MagicMock(spec=stripe.Event)
    ev.type = "charge.refunded"
    ev.data = MagicMock()
    ev.data.object = {
        "id": "ch_test",
        "payment_intent": payment_intent_id,
        "refunds": {"data": refunds},
    }
    # Make dict(event) serializable — tests shouldn't care about exact shape,
    # so patch out raw_response assignment in the handler by providing __iter__.
    ev.__iter__.return_value = iter([])
    return ev


def _batch(payments: list[Payment], intent_id: str) -> None:
    Payment.objects.filter(pk__in=[p.pk for p in payments]).update(
        stripe_payment_intent_id=intent_id, status=Payment.PaymentStatus.SUCCEEDED
    )


class TestChargeRefundedMatching:
    def test_branch_1_existing_stripe_refund_id_match(self, batch_of_4_online_payments):
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        target = payments[1]
        target.stripe_refund_id = "re_already_recorded"
        target.refund_status = Payment.RefundStatus.PENDING
        target.save(update_fields=["stripe_refund_id", "refund_status"])

        refund = {"id": "re_already_recorded", "amount": 4000, "metadata": {}}
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )

        target.refresh_from_db()
        assert target.refund_status == Payment.RefundStatus.SUCCEEDED
        assert target.status == Payment.PaymentStatus.REFUNDED
        for other in payments:
            if other.pk == target.pk:
                continue
            other.refresh_from_db()
            assert other.status == Payment.PaymentStatus.SUCCEEDED, "other payments must be untouched"

    def test_branch_2_metadata_ticket_id_match(self, batch_of_4_online_payments):
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        target = payments[2]
        refund = {
            "id": "re_new",
            "amount": 4000,
            "metadata": {"ticket_id": str(target.ticket_id)},
        }
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )
        target.refresh_from_db()
        assert target.refund_status == Payment.RefundStatus.SUCCEEDED
        target.ticket.refresh_from_db()
        assert target.ticket.status == Ticket.TicketStatus.CANCELLED
        assert target.ticket.cancellation_source == "stripe_dashboard"
        for other in payments:
            if other.pk == target.pk:
                continue
            other.refresh_from_db()
            assert other.status == Payment.PaymentStatus.SUCCEEDED

    def test_branch_3_exact_amount_unambiguous(self, batch_of_4_online_payments):
        payments = batch_of_4_online_payments
        # Mix amounts so exactly one matches.
        payments[0].amount = Decimal("50.00")
        payments[0].save(update_fields=["amount"])
        _batch(payments, "pi_batch")

        refund = {"id": "re_new", "amount": 5000, "metadata": {}}
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )
        payments[0].refresh_from_db()
        assert payments[0].refund_status == Payment.RefundStatus.SUCCEEDED

    def test_branch_4_full_intent_refund_applies_to_all(self, batch_of_4_online_payments):
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        total_cents = int(sum(p.amount for p in payments) * 100)
        refund = {"id": "re_full", "amount": total_cents, "metadata": {}}
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )
        for p in payments:
            p.refresh_from_db()
            assert p.refund_status == Payment.RefundStatus.SUCCEEDED
            assert p.status == Payment.PaymentStatus.REFUNDED
            p.ticket.refresh_from_db()
            assert p.ticket.status == Ticket.TicketStatus.CANCELLED

    def test_branch_5_ambiguous_logged_no_mutation(self, batch_of_4_online_payments, caplog):
        payments = batch_of_4_online_payments
        # All payments cost the same AND amount doesn't equal full intent.
        _batch(payments, "pi_batch")
        refund = {"id": "re_ambig", "amount": 4000, "metadata": {}}  # matches any single payment
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )
        for p in payments:
            p.refresh_from_db()
            assert p.status == Payment.PaymentStatus.SUCCEEDED
            assert p.refund_status is None

    def test_duplicate_webhook_is_idempotent(self, batch_of_4_online_payments):
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        target = payments[0]
        target.stripe_refund_id = "re_a"
        target.refund_status = Payment.RefundStatus.SUCCEEDED
        target.status = Payment.PaymentStatus.REFUNDED
        target.save(update_fields=["stripe_refund_id", "refund_status", "status"])
        refund = {"id": "re_a", "amount": int(target.amount * 100), "metadata": {}}
        # Replay — should be a no-op.
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )
        target.refresh_from_db()
        assert target.status == Payment.PaymentStatus.REFUNDED
```

Add a fixture to the surrounding `conftest.py`:

```python
@pytest.fixture
def batch_of_4_online_payments(payment_factory, ticket_factory, tier_online_with_cancellation_enabled):
    payments = []
    for _ in range(4):
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled)
        p = payment_factory(ticket=ticket, amount=Decimal("40.00"))
        payments.append(p)
    return payments
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest src/events/tests/test_controllers/test_stripe_webhook_refund_matching.py -v`
Expected: all fail (existing handler cascades).

### Task 6.2: Refactor `handle_charge_refunded`

**Files:**
- Modify: `src/events/service/stripe_webhooks.py:157-247`

Replace the existing `handle_charge_refunded` with the per-refund matching strategy.

- [ ] **Step 1: Rewrite the method**

Replace the `handle_charge_refunded` method body with:

```python
    @transaction.atomic
    def handle_charge_refunded(self, event: stripe.Event) -> None:
        """Match each refund object in the charge to its specific Payment row.

        Five-branch matching strategy (first match wins):
          1. existing stripe_refund_id on a Payment
          2. refund.metadata["ticket_id"]
          3. exactly one unrefunded Payment with matching amount
          4. refund.amount equals sum of unrefunded-payment amounts (full remaining batch)
          5. ambiguous → logged, no mutation
        """
        charge_data = event.data.object
        payment_intent_id = charge_data.get("payment_intent")

        if not payment_intent_id:
            logger.warning("stripe_refund_missing_intent", charge_id=charge_data.get("id"))
            return

        candidates = list(
            Payment.objects.filter(stripe_payment_intent_id=payment_intent_id).select_related(
                "ticket", "ticket__tier"
            )
        )
        if not candidates:
            logger.warning("stripe_refund_unknown_intent", payment_intent_id=payment_intent_id)
            return

        refunds = charge_data.get("refunds", {}).get("data", []) or []
        if not refunds:
            logger.warning("stripe_refund_event_no_refund_data", payment_intent_id=payment_intent_id)
            return

        raw_response = dict(event)
        touched_session_id: str | None = None
        newly_refunded_ids: list[str] = []

        for refund in refunds:
            matched = self._match_refund_to_payments(refund, candidates)
            if not matched:
                logger.warning(
                    "stripe_refund_ambiguous_match",
                    payment_intent_id=payment_intent_id,
                    refund_id=refund.get("id"),
                    refund_amount=refund.get("amount"),
                    candidate_payment_ids=[str(c.id) for c in candidates if c.refund_status is None],
                )
                continue
            for payment in matched:
                if payment.refund_status == Payment.RefundStatus.SUCCEEDED:
                    continue  # idempotent replay
                self._apply_refund_to_payment(payment, refund, raw_response)
                newly_refunded_ids.append(str(payment.id))
                touched_session_id = payment.stripe_session_id

        if touched_session_id and newly_refunded_ids:
            sid, ids = touched_session_id, newly_refunded_ids

            def _trigger_credit_note() -> None:
                from events.tasks import generate_attendee_credit_note_task

                generate_attendee_credit_note_task.delay(sid, ids)

            transaction.on_commit(_trigger_credit_note)

        logger.info(
            "stripe_refund_processed",
            payment_intent_id=payment_intent_id,
            refund_count=len(refunds),
            newly_refunded_payment_ids=newly_refunded_ids,
        )

    def _match_refund_to_payments(
        self, refund: dict, candidates: list[Payment]
    ) -> list[Payment]:
        """Return the Payment(s) this refund should apply to. Empty list = no match."""
        refund_id = refund.get("id")
        refund_amount = int(refund.get("amount", 0))

        # Branch 1: already-known refund id.
        for p in candidates:
            if p.stripe_refund_id and p.stripe_refund_id == refund_id:
                return [p]

        # Branch 2: explicit metadata pointer.
        metadata_ticket_id = (refund.get("metadata") or {}).get("ticket_id")
        if metadata_ticket_id:
            for p in candidates:
                if str(p.ticket_id) == metadata_ticket_id:
                    return [p]

        unrefunded = [p for p in candidates if p.refund_status is None]
        if not unrefunded:
            return []

        # Branch 3: exactly-one exact-amount match among unrefunded rows.
        exact = [p for p in unrefunded if int(p.amount * 100) == refund_amount]
        if len(exact) == 1:
            return exact

        # Branch 4: full-remaining-batch refund.
        remaining_total = sum(int(p.amount * 100) for p in unrefunded)
        if refund_amount == remaining_total:
            return unrefunded

        # Branch 5: ambiguous.
        return []

    def _apply_refund_to_payment(self, payment: Payment, refund: dict, raw_response: dict) -> None:
        from django.utils import timezone as _tz

        from events.models.ticket import CancellationSource

        payment.stripe_refund_id = refund["id"]
        payment.refund_amount = Decimal(refund["amount"]) / Decimal(100)
        payment.refund_status = Payment.RefundStatus.SUCCEEDED
        payment.refunded_at = _tz.now()
        payment.status = Payment.PaymentStatus.REFUNDED
        payment.raw_response = raw_response
        payment.save(
            update_fields=[
                "stripe_refund_id",
                "refund_amount",
                "refund_status",
                "refunded_at",
                "status",
                "raw_response",
            ]
        )

        ticket = payment.ticket
        if ticket.status != Ticket.TicketStatus.CANCELLED:
            ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
            ticket._refund_amount = f"{payment.refund_amount} {payment.currency}"  # type: ignore[attr-defined]
            ticket.status = Ticket.TicketStatus.CANCELLED
            ticket.cancelled_at = _tz.now()
            ticket.cancellation_source = CancellationSource.STRIPE_DASHBOARD
            ticket.save(
                update_fields=["status", "cancelled_at", "cancellation_source"]
            )
            TicketTier.objects.filter(pk=ticket.tier_id, quantity_sold__gt=0).update(
                quantity_sold=F("quantity_sold") - 1
            )
```

Also add the imports at the top of the file: `from decimal import Decimal` and ensure `Payment`, `Ticket`, `TicketTier` stay in the existing import line.

- [ ] **Step 2: Run the new matching tests**

Run: `uv run pytest src/events/tests/test_controllers/test_stripe_webhook_refund_matching.py -v`
Expected: pass.

- [ ] **Step 3: Run the existing webhook tests**

Run: `uv run pytest src/events/tests/test_controllers/test_stripe_webhook.py -v`
Expected: still pass (no regressions for non-batch cases). If any fail, they likely asserted the old cascade-by-intent behavior — update the test assertions to match the new specific-payment matching. Do **not** soften the new matching strategy to match old tests.

- [ ] **Step 4: Commit (ask user first)**

```bash
git add src/events/service/stripe_webhooks.py src/events/tests/test_controllers/test_stripe_webhook_refund_matching.py src/events/tests/test_controllers/test_stripe_webhook.py
git status
```

Draft message:

```
fix(events)!: match stripe refunds per-payment, not per-intent (#370)

The previous handle_charge_refunded blanket-cascaded any refund on a
shared payment_intent_id to every Payment row, which was wrong for
batch partial refunds. Replaces it with a five-branch match:
stripe_refund_id → metadata.ticket_id → exact-unambiguous amount →
full-intent → ambiguous (logged, no mutation). Dashboard-initiated
cancellations now set cancellation_source=STRIPE_DASHBOARD.

Required for user-initiated ticket cancellation in the same feature.
```

---

## Phase 7 — User-facing endpoints

### Task 7.1: Add new schemas

**Files:**
- Modify: `src/events/schema/ticket.py`
- Modify: `src/events/schema/__init__.py`

- [ ] **Step 1: Add the schemas near the bottom of `events/schema/ticket.py`**

Insert before `# --- Stripe Schemas ---`:

```python
# ---- Cancellation Schemas ----


class RefundPolicyTierSchema(Schema):
    hours_before_event: int = Field(ge=0)
    refund_percentage: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)


class RefundPolicySchema(Schema):
    tiers: list[RefundPolicyTierSchema] = Field(min_length=1)
    flat_fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)

    @model_validator(mode="after")
    def _validate_monotonic(self) -> t.Self:
        for i in range(1, len(self.tiers)):
            prev = self.tiers[i - 1]
            curr = self.tiers[i]
            if curr.hours_before_event >= prev.hours_before_event:
                raise ValueError("hours_before_event must be strictly descending across tiers")
            if curr.refund_percentage > prev.refund_percentage:
                raise ValueError("refund_percentage must be monotonically non-increasing across tiers")
        return self


class RefundWindowSchema(Schema):
    refund_percentage: Decimal
    refund_amount: Decimal
    effective_until: AwareDatetime


class CancellationPreviewSchema(Schema):
    can_cancel: bool
    reason: models.ticket.CancellationBlockReason | None = None
    refund_amount: Decimal
    currency: str
    deadline: AwareDatetime | None = None
    flat_fee: Decimal
    payment_method: TicketTier.PaymentMethod
    windows: list[RefundWindowSchema] = Field(default_factory=list)
    policy_snapshot: RefundPolicySchema | None = None


class TicketCancellationRequestSchema(Schema):
    reason: StrippedString | None = Field(default=None, max_length=500)


class TicketCancellationResponseSchema(Schema):
    ticket: UserTicketSchema
    refund_amount: Decimal
    currency: str
    refund_status: Payment.RefundStatus | None = None


class CancellationBlockedErrorSchema(Schema):
    code: models.ticket.CancellationBlockReason
    detail: str


class AdminCancelTicketSchema(Schema):
    """Optional payload for admin cancel / mark-refunded endpoints."""

    cancellation_reason: StrippedString | None = Field(default=None, max_length=500)
```

Make sure `from events.models import ticket as _ticket_models` (or `from events import models` already in-file — it is) grants access to `models.ticket.CancellationBlockReason`. The existing import `from events import models` at line 13 is sufficient.

- [ ] **Step 2: Extend `TicketTierCreateSchema` + `TicketTierUpdateSchema` with the three new fields**

Inside `TicketTierCreateSchema` add near the bottom (before the `@model_validator` methods):

```python
    allow_user_cancellation: bool = False
    cancellation_deadline_hours: int | None = Field(default=None, ge=0)
    refund_policy: RefundPolicySchema | None = None
```

Inside `TicketTierUpdateSchema` add:

```python
    allow_user_cancellation: bool | None = None
    cancellation_deadline_hours: int | None = Field(default=None, ge=0)
    refund_policy: RefundPolicySchema | None = None
```

Note: `refund_policy` on `TicketTierUpdateSchema` can't distinguish "unset" from "clear to null" with `None | RefundPolicySchema`. Keep the `model_dump(exclude_unset=True)` pattern in the service so callers who want to clear explicitly pass `None` via a JSON `null` — Pydantic will mark the field as set.

- [ ] **Step 3: Extend `TicketTierDetailSchema` to surface the new fields**

Inside `TicketTierDetailSchema.Meta.fields`, append `"allow_user_cancellation"` and `"cancellation_deadline_hours"`. Also add a class-level field declaration for `refund_policy`:

```python
    refund_policy: RefundPolicySchema | None = None
```

- [ ] **Step 4: Update `events/schema/__init__.py` re-exports**

In the ticket-schema `from .ticket import (...)` block, add:

```python
    AdminCancelTicketSchema,
    CancellationBlockedErrorSchema,
    CancellationPreviewSchema,
    RefundPolicySchema,
    RefundPolicyTierSchema,
    RefundWindowSchema,
    TicketCancellationRequestSchema,
    TicketCancellationResponseSchema,
```

And add each to the `__all__` list in alphabetical order.

- [ ] **Step 5: Run the mypy/check suite**

Run: `make check`
Expected: passes.

### Task 7.2: Add the two new endpoints on `EventPublicTicketsController`

**Files:**
- Modify: `src/events/controllers/event_public/tickets.py`

- [ ] **Step 1: Add the preview + cancel endpoints**

At the bottom of `EventPublicTicketsController`, after the last existing `@route` method, add:

```python
    @route.get(
        "/tickets/{ticket_id}/cancellation-preview",
        url_name="ticket_cancellation_preview",
        response={200: schema.CancellationPreviewSchema, 403: schema.ResponseMessage},
        auth=I18nJWTAuth(),  # authenticated only
    )
    def cancellation_preview(self, ticket_id: UUID) -> schema.CancellationPreviewSchema:
        """Preview the refund the ticket holder would get if they cancelled now.

        Returns 403 if the caller is not the ticket owner.
        When ``can_cancel`` is False, ``reason`` is populated with a stable error code.
        """
        from django.utils import timezone

        from events.service.cancellation_service import build_cancellation_preview

        ticket = get_object_or_404(models.Ticket.objects.full(), pk=ticket_id)
        user = self.context.request.user  # type: ignore[union-attr]
        if ticket.user_id != user.id:
            raise HttpError(403, str(_("Only the ticket holder can view this preview.")))

        preview = build_cancellation_preview(ticket, timezone.now())
        return schema.CancellationPreviewSchema(
            can_cancel=preview.can_cancel,
            reason=preview.reason,
            refund_amount=preview.refund_amount,
            currency=preview.currency,
            deadline=preview.deadline,
            flat_fee=preview.flat_fee,
            payment_method=models.TicketTier.PaymentMethod(preview.payment_method),
            windows=[
                schema.RefundWindowSchema(
                    refund_percentage=w.refund_percentage,
                    refund_amount=w.refund_amount,
                    effective_until=w.effective_until,
                )
                for w in preview.windows
            ],
            policy_snapshot=(
                schema.RefundPolicySchema.model_validate(preview.policy_snapshot.model_dump())
                if preview.policy_snapshot is not None
                else None
            ),
        )

    @route.post(
        "/tickets/{ticket_id}/cancel",
        url_name="cancel_my_ticket",
        response={
            200: schema.TicketCancellationResponseSchema,
            403: schema.ResponseMessage,
            409: schema.CancellationBlockedErrorSchema,
            502: schema.ResponseMessage,
        },
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def cancel_my_ticket(
        self,
        ticket_id: UUID,
        payload: schema.TicketCancellationRequestSchema,
    ) -> schema.TicketCancellationResponseSchema:
        """Ticket-holder-initiated cancellation (+ automatic Stripe refund where applicable)."""
        from django.utils import timezone

        from events.service.cancellation_service import (
            CancellationBlocked,
            CancellationNotOwner,
            StripeRefundFailed,
            cancel_ticket_by_user,
        )

        ticket = get_object_or_404(models.Ticket.objects.full(), pk=ticket_id)
        user = self.context.request.user  # type: ignore[union-attr]

        try:
            result = cancel_ticket_by_user(
                ticket=ticket,
                user=user,
                reason=payload.reason or "",
                now=timezone.now(),
            )
        except CancellationNotOwner as exc:
            raise HttpError(403, str(_("Only the ticket holder can cancel this ticket."))) from exc
        except CancellationBlocked as exc:
            raise HttpError(
                409,
                schema.CancellationBlockedErrorSchema(
                    code=exc.reason, detail=str(exc.reason.label)
                ).model_dump_json(),
            ) from exc
        except StripeRefundFailed as exc:
            raise HttpError(502, str(_("Refund failed. Please try again later."))) from exc

        fresh = models.Ticket.objects.full().get(pk=result.ticket.pk)
        return schema.TicketCancellationResponseSchema(
            ticket=schema.UserTicketSchema.from_orm(fresh),
            refund_amount=result.refund_amount,
            currency=result.currency,
            refund_status=(
                models.Payment.RefundStatus(result.refund_status) if result.refund_status else None
            ),
        )
```

Notes:
- `ResponseMessage` is already imported at line 12. If the user routes 409 through ResponseMessage, frontend would lose structured data — so we pipe `CancellationBlockedErrorSchema.model_dump_json()` as the body and keep 409 mapped to it.
- If Django Ninja rejects the approach of raising `HttpError(409, json_string)` because its detail field is stringly-typed, swap to returning the error tuple: `return 409, schema.CancellationBlockedErrorSchema(...)` and change the return type annotation accordingly. Pick whichever pattern the codebase already uses — grep `raise HttpError(409` in the existing controllers first.

### Task 7.3: Write integration tests for the new endpoints

**Files:**
- Create: `src/events/tests/test_controllers/test_event_public/test_ticket_cancellation.py`

- [ ] **Step 1: Inspect existing public-controller tests**

Open `src/events/tests/test_controllers/test_event_stripe_checkout.py` to see the existing patterns for authenticated requests against `event_public` controllers and Stripe mocking.

- [ ] **Step 2: Write the test file**

```python
"""Integration tests for GET /cancellation-preview and POST /cancel."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from events.models import Payment, Ticket, TicketTier

pytestmark = pytest.mark.django_db


@pytest.fixture
def online_cancellable_tier(tier_factory, event):
    event.start = timezone.now() + timedelta(hours=72)
    event.save(update_fields=["start"])
    return tier_factory(
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price=Decimal("40.00"),
        allow_user_cancellation=True,
        refund_policy={
            "tiers": [
                {"hours_before_event": 48, "refund_percentage": "100"},
                {"hours_before_event": 24, "refund_percentage": "50"},
            ],
            "flat_fee": "0",
        },
    )


class TestCancellationPreview:
    def test_returns_windows_and_live_quote(self, client_for, online_cancellable_tier, ticket_factory, payment_factory):
        ticket = ticket_factory(
            tier=online_cancellable_tier, refund_policy_snapshot=online_cancellable_tier.refund_policy
        )
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        url = reverse("api:ticket_cancellation_preview", kwargs={"ticket_id": str(ticket.id)})
        resp = client_for(ticket.user).get(url)
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_cancel"] is True
        assert Decimal(body["refund_amount"]) == Decimal("40.00")
        assert len(body["windows"]) == 2

    def test_403_for_non_owner(self, client_for, user_factory, online_cancellable_tier, ticket_factory):
        ticket = ticket_factory(tier=online_cancellable_tier)
        other = user_factory()
        url = reverse("api:ticket_cancellation_preview", kwargs={"ticket_id": str(ticket.id)})
        resp = client_for(other).get(url)
        assert resp.status_code == 403

    def test_returns_reason_when_event_started(self, client_for, online_cancellable_tier, ticket_factory, event):
        event.start = timezone.now() - timedelta(minutes=5)
        event.save(update_fields=["start"])
        ticket = ticket_factory(tier=online_cancellable_tier)
        url = reverse("api:ticket_cancellation_preview", kwargs={"ticket_id": str(ticket.id)})
        resp = client_for(ticket.user).get(url)
        assert resp.status_code == 200
        assert resp.json()["can_cancel"] is False
        assert resp.json()["reason"] == "event_started"


class TestCancelMyTicket:
    def test_online_ticket_happy_path(
        self, client_for, online_cancellable_tier, ticket_factory, payment_factory
    ):
        ticket = ticket_factory(
            tier=online_cancellable_tier, refund_policy_snapshot=online_cancellable_tier.refund_policy
        )
        payment = payment_factory(
            ticket=ticket, amount=Decimal("40.00"), stripe_payment_intent_id="pi_ok"
        )
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        with patch("stripe.Refund.create") as mock:
            mock.return_value = MagicMock(id="re_ok")
            resp = client_for(ticket.user).post(
                url, data={"reason": "double-booked"}, content_type="application/json"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert Decimal(body["refund_amount"]) == Decimal("40.00")
        assert body["refund_status"] == "pending"
        payment.refresh_from_db()
        ticket.refresh_from_db()
        assert ticket.status == "cancelled"
        assert ticket.cancellation_source == "user"
        assert ticket.cancellation_reason == "double-booked"
        assert payment.refund_status == "pending"

    def test_free_ticket_no_stripe_call(self, client_for, tier_factory, ticket_factory, event):
        event.start = timezone.now() + timedelta(hours=72)
        event.save(update_fields=["start"])
        tier = tier_factory(
            payment_method=TicketTier.PaymentMethod.FREE,
            price=Decimal("0"),
            allow_user_cancellation=True,
        )
        ticket = ticket_factory(tier=tier)
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        with patch("stripe.Refund.create") as mock:
            resp = client_for(ticket.user).post(url, data={}, content_type="application/json")
        assert resp.status_code == 200
        assert mock.call_count == 0
        assert resp.json()["refund_status"] is None

    def test_blocks_with_409_when_already_cancelled(
        self, client_for, online_cancellable_tier, ticket_factory
    ):
        ticket = ticket_factory(tier=online_cancellable_tier, status=Ticket.TicketStatus.CANCELLED)
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        resp = client_for(ticket.user).post(url, data={}, content_type="application/json")
        assert resp.status_code == 409
        # Body can be either JSON-string-inside-detail or structured, depending on controller shape.
        # Both must contain the stable code.
        assert "already_cancelled" in resp.content.decode()

    def test_403_for_non_owner(self, client_for, user_factory, online_cancellable_tier, ticket_factory):
        ticket = ticket_factory(tier=online_cancellable_tier)
        other = user_factory()
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        resp = client_for(other).post(url, data={}, content_type="application/json")
        assert resp.status_code == 403

    def test_stripe_failure_returns_502_and_ticket_stays_active(
        self, client_for, online_cancellable_tier, ticket_factory, payment_factory
    ):
        import stripe
        ticket = ticket_factory(
            tier=online_cancellable_tier, refund_policy_snapshot=online_cancellable_tier.refund_policy
        )
        payment_factory(ticket=ticket, amount=Decimal("40.00"), stripe_payment_intent_id="pi_fail")
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        with patch("stripe.Refund.create", side_effect=stripe.error.APIError("boom")):
            resp = client_for(ticket.user).post(url, data={}, content_type="application/json")
        assert resp.status_code == 502
        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.ACTIVE

    def test_idempotent_duplicate_returns_409(
        self, client_for, online_cancellable_tier, ticket_factory, payment_factory
    ):
        ticket = ticket_factory(
            tier=online_cancellable_tier,
            refund_policy_snapshot=online_cancellable_tier.refund_policy,
            status=Ticket.TicketStatus.CANCELLED,
        )
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        resp = client_for(ticket.user).post(url, data={}, content_type="application/json")
        assert resp.status_code == 409
```

If `client_for` / `user_factory` are not conftest-provided, adapt to the existing pattern. Inspect an existing authenticated public-controller test (e.g. `test_event_stripe_checkout.py`) for the actual fixture names and mimic them.

- [ ] **Step 3: Run tests**

Run: `uv run pytest src/events/tests/test_controllers/test_event_public/test_ticket_cancellation.py -v`
Expected: pass.

- [ ] **Step 4: Commit (ask user first)**

```bash
git add src/events/controllers/event_public/tickets.py src/events/schema/ src/events/tests/test_controllers/test_event_public/test_ticket_cancellation.py
git status
```

Draft message:

```
feat(events): user-facing cancel + cancellation-preview endpoints (#370)

GET /events/tickets/{id}/cancellation-preview returns the live quote +
refund-curve windows. POST /events/tickets/{id}/cancel runs the full
Stripe-refund + DB mutation flow atomically. 409 carries a stable
CancellationBlockReason code for frontend i18n.
```

---

## Phase 8 — Tier admin endpoints accept new fields

### Task 8.1: Thread the three new fields through the ticket-tier service

**Files:**
- Modify: `src/events/service/ticket_service.py` — functions `create_ticket_tier` and `update_ticket_tier`.

- [ ] **Step 1: Inspect the functions**

Open `src/events/service/ticket_service.py`, find `create_ticket_tier` and `update_ticket_tier`. They already accept `tier_data: dict[str, Any]` from the controller. No code change should be needed if they iterate `tier_data` and `setattr` — verify and otherwise extend to pass `allow_user_cancellation`, `cancellation_deadline_hours`, and `refund_policy` through to the model.

- [ ] **Step 2: If they pass through already, add a lightweight unit test that a valid policy round-trips**

`src/events/tests/test_service/test_ticket_tier_cancellation_fields.py`:

```python
from decimal import Decimal

import pytest

from events.service import ticket_service

pytestmark = pytest.mark.django_db


def test_create_ticket_tier_persists_cancellation_fields(event):
    tier = ticket_service.create_ticket_tier(
        event=event,
        tier_data={
            "name": "VIP",
            "price": Decimal("50"),
            "allow_user_cancellation": True,
            "cancellation_deadline_hours": 24,
            "refund_policy": {
                "tiers": [{"hours_before_event": 24, "refund_percentage": "50"}],
                "flat_fee": "0",
            },
        },
        restricted_to_membership_tiers_ids=None,
    )
    tier.refresh_from_db()
    assert tier.allow_user_cancellation is True
    assert tier.cancellation_deadline_hours == 24
    assert tier.refund_policy["tiers"][0]["refund_percentage"] == "50"


def test_update_ticket_tier_clears_refund_policy(tier_factory):
    tier = tier_factory(
        refund_policy={
            "tiers": [{"hours_before_event": 0, "refund_percentage": "50"}],
            "flat_fee": "0",
        }
    )
    ticket_service.update_ticket_tier(
        tier=tier, tier_data={"refund_policy": None}, restricted_to_membership_tiers_ids=None
    )
    tier.refresh_from_db()
    assert tier.refund_policy is None
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest src/events/tests/test_service/test_ticket_tier_cancellation_fields.py -v`
Expected: pass. If update doesn't clear, fix `update_ticket_tier` so it calls `setattr(tier, key, value)` for each key in `tier_data` (values of None included, since pydantic's `exclude_unset=True` guarantees only explicitly-set fields are present).

- [ ] **Step 4: Commit (ask user first)**

```bash
git add src/events/service/ticket_service.py src/events/tests/test_service/test_ticket_tier_cancellation_fields.py src/events/schema/
git status
```

Draft message:

```
feat(events): tier admin accepts cancellation & refund policy fields (#370)

Extends TicketTierCreate/UpdateSchema with allow_user_cancellation,
cancellation_deadline_hours, and refund_policy. The service layer
already threads arbitrary fields to the model; this change surfaces
the three new fields in the API contract.
```

---

## Phase 9 — Admin cancellation audit-field backfill + mark-refunded fix

### Task 9.1: Backfill audit fields on admin endpoints

**Files:**
- Modify: `src/events/controllers/event_admin/tickets.py`

- [ ] **Step 1: Update `cancel_ticket` to accept an optional reason and populate audit fields**

Replace the existing `cancel_ticket` method body (around lines 293-331) with:

```python
    @route.post(
        "/tickets/{ticket_id}/cancel",
        url_name="cancel_ticket",
        response={200: schema.UserTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def cancel_ticket(
        self,
        event_id: UUID,
        ticket_id: UUID,
        payload: schema.AdminCancelTicketSchema | None = Body(None),  # type: ignore[type-arg]
    ) -> models.Ticket:
        """Cancel a manual payment ticket.

        This endpoint is for offline/at-the-door tickets only.
        Online tickets (Stripe) should be refunded via Stripe Dashboard.
        """
        from django.utils import timezone

        from events.models.ticket import CancellationSource

        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("tier"),
            pk=ticket_id,
            event=event,
            tier__payment_method__in=[
                models.TicketTier.PaymentMethod.OFFLINE,
                models.TicketTier.PaymentMethod.AT_THE_DOOR,
            ],
        )

        if ticket.status == models.Ticket.TicketStatus.CANCELLED:
            raise HttpError(400, str(_("Ticket already cancelled")))

        reason = (payload.cancellation_reason if payload else None) or ""

        with transaction.atomic():
            models.TicketTier.objects.select_for_update().filter(
                pk=ticket.tier.pk, quantity_sold__gt=0
            ).update(quantity_sold=F("quantity_sold") - 1)
            ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
            ticket.status = models.Ticket.TicketStatus.CANCELLED
            ticket.cancelled_at = timezone.now()
            ticket.cancelled_by = self.user()
            ticket.cancellation_source = CancellationSource.ORGANIZER
            ticket.cancellation_reason = reason
            ticket.save(
                update_fields=[
                    "status",
                    "cancelled_at",
                    "cancelled_by",
                    "cancellation_source",
                    "cancellation_reason",
                ]
            )

        return models.Ticket.objects.full().get(pk=ticket.pk)
```

- [ ] **Step 2: Update `mark_ticket_refunded` similarly**

Replace its body (around lines 253-291) with:

```python
    @route.post(
        "/tickets/{ticket_id}/mark-refunded",
        url_name="mark_ticket_refunded",
        response={200: schema.UserTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def mark_ticket_refunded(
        self,
        event_id: UUID,
        ticket_id: UUID,
        payload: schema.AdminCancelTicketSchema | None = Body(None),  # type: ignore[type-arg]
    ) -> models.Ticket:
        """Mark a manual payment ticket as refunded and cancel it."""
        from django.utils import timezone

        from events.models.ticket import CancellationSource

        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("tier", "payment"),
            pk=ticket_id,
            event=event,
            tier__payment_method__in=[
                models.TicketTier.PaymentMethod.OFFLINE,
                models.TicketTier.PaymentMethod.AT_THE_DOOR,
            ],
        )
        reason = (payload.cancellation_reason if payload else None) or ""

        with transaction.atomic():
            models.TicketTier.objects.select_for_update().filter(
                pk=ticket.tier.pk, quantity_sold__gt=0
            ).update(quantity_sold=F("quantity_sold") - 1)

            ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
            ticket.status = models.Ticket.TicketStatus.CANCELLED
            ticket.cancelled_at = timezone.now()
            ticket.cancelled_by = self.user()
            ticket.cancellation_source = CancellationSource.ORGANIZER
            ticket.cancellation_reason = reason
            ticket.save(
                update_fields=[
                    "status",
                    "cancelled_at",
                    "cancelled_by",
                    "cancellation_source",
                    "cancellation_reason",
                ]
            )

            if hasattr(ticket, "payment"):
                ticket.payment.status = models.Payment.PaymentStatus.REFUNDED
                ticket.payment.refund_amount = ticket.payment.amount
                ticket.payment.refund_status = models.Payment.RefundStatus.SUCCEEDED
                ticket.payment.refunded_at = timezone.now()
                ticket.payment.save(
                    update_fields=[
                        "status",
                        "refund_amount",
                        "refund_status",
                        "refunded_at",
                    ]
                )

        return models.Ticket.objects.full().get(pk=ticket.pk)
```

- [ ] **Step 3: Add regression tests**

Append to `src/events/tests/test_controllers/test_event_admin/test_tickets/test_ticket_refund_cancel.py`:

```python
def test_admin_cancel_populates_audit_fields(client_admin, event_with_offline_tier, ticket_factory):
    ticket = ticket_factory(tier=event_with_offline_tier.tier)
    url = reverse(
        "api:cancel_ticket",
        kwargs={"event_id": str(event_with_offline_tier.event.id), "ticket_id": str(ticket.id)},
    )
    resp = client_admin.post(url, data={"cancellation_reason": "no-show"}, content_type="application/json")
    assert resp.status_code == 200
    ticket.refresh_from_db()
    assert ticket.cancellation_source == "organizer"
    assert ticket.cancellation_reason == "no-show"
    assert ticket.cancelled_at is not None
    assert ticket.cancelled_by_id == client_admin.admin_user.id


def test_admin_mark_refunded_fires_cancelled_signal(
    client_admin, event_with_offline_tier, ticket_factory, payment_factory, django_capture_on_commit_callbacks
):
    """Regression: mark_ticket_refunded used to skip _original_ticket_status so no TICKET_CANCELLED fired."""
    ticket = ticket_factory(tier=event_with_offline_tier.tier)
    payment_factory(ticket=ticket, amount=Decimal("40.00"))
    url = reverse(
        "api:mark_ticket_refunded",
        kwargs={"event_id": str(event_with_offline_tier.event.id), "ticket_id": str(ticket.id)},
    )
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        resp = client_admin.post(url, data={}, content_type="application/json")
    assert resp.status_code == 200
    # The signal handler queues an on_commit callback when _original_ticket_status transitions to CANCELLED.
    assert len(callbacks) >= 1
```

(If `django_capture_on_commit_callbacks` is not already in use, substitute with a direct assertion against a patched `notification_requested.send` — grep `notification_requested` in the existing tests for the pattern.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest src/events/tests/test_controllers/test_event_admin/test_tickets/test_ticket_refund_cancel.py -v`
Expected: pass.

- [ ] **Step 5: Commit (ask user first)**

```bash
git add src/events/controllers/event_admin/tickets.py src/events/tests/test_controllers/test_event_admin/test_tickets/test_ticket_refund_cancel.py
git status
```

Draft message:

```
fix(events): admin cancel + mark-refunded populate audit fields (#370)

cancel_ticket and mark_ticket_refunded now record cancelled_at,
cancelled_by, cancellation_source=organizer, and the optional
cancellation_reason. mark_ticket_refunded also captures
_original_ticket_status so TICKET_CANCELLED notifications fire
(regression fix — previously silently skipped).
```

---

## Phase 10 — Notification fixes & template copy

### Task 10.1: Fix `_send_refund_notifications` to honor `refund_amount`

**Files:**
- Modify: `src/notifications/signals/payment.py:118`

- [ ] **Step 1: Replace the hardcoded `payment.amount`**

Change line 118 from:

```python
refund_amount = f"{payment.amount} {payment.currency}"
```

to:

```python
actual_amount = payment.refund_amount if payment.refund_amount is not None else payment.amount
refund_amount = f"{actual_amount} {payment.currency}"
```

- [ ] **Step 2: Add a regression test**

Append to `src/notifications/tests/test_signals.py` (or an equivalent payment-signals test file — grep first):

```python
def test_refund_notification_reports_partial_refund_amount(payment_factory, ticket_factory):
    from django.db import transaction
    from unittest.mock import patch

    ticket = ticket_factory()
    payment = payment_factory(
        ticket=ticket,
        amount=Decimal("40.00"),
        status=Payment.PaymentStatus.SUCCEEDED,
    )
    payment.refund_amount = Decimal("20.00")
    with patch("notifications.signals.payment.notification_requested.send") as send_mock:
        payment.status = Payment.PaymentStatus.REFUNDED
        with transaction.atomic():
            payment.save()
    contexts = [call.kwargs["context"] for call in send_mock.call_args_list]
    assert any("20.00" in c["refund_amount"] for c in contexts)
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest src/notifications/tests/test_signals.py -v -k refund`
Expected: pass.

### Task 10.2: Surface cancellation source + reason in the ticket-cancelled context

**Files:**
- Modify: `src/notifications/signals/ticket.py` — `_build_ticket_updated_context`

- [ ] **Step 1: Extend the context builder**

At the end of `_build_ticket_updated_context` (around line 118), add:

```python
    context["cancellation_source"] = ticket.cancellation_source or ""
    context["cancellation_reason"] = ticket.cancellation_reason or ""
```

- [ ] **Step 2: Update templates to branch on source**

Edit the three `ticket_cancelled` templates to branch on `context.cancellation_source`:

`src/notifications/templates/notifications/in_app/ticket_cancelled.md`: Replace the existing first line with:

```
{% load i18n %}{% if context.cancellation_source == "user" %}{% blocktranslate with event=context.event_name %}You cancelled your ticket for **{{ event }}**.{% endblocktranslate %}{% elif context.cancellation_source == "stripe_dashboard" %}{% blocktranslate with event=context.event_name %}Your ticket for **{{ event }}** has been cancelled and refunded.{% endblocktranslate %}{% else %}{% blocktranslate with event=context.event_name %}Your ticket for **{{ event }}** has been cancelled.{% endblocktranslate %}{% endif %}
```

(Keep everything below unchanged — it already renders `context.cancellation_reason` conditionally.)

Apply the analogous branching to `telegram/ticket_cancelled.md` and both `email/ticket_cancelled.{html,txt}` templates. Inspect each file first so existing markdown/HTML structure stays intact; only swap the first-line header sentence.

- [ ] **Step 3: Update i18n**

Run: `make makemessages`
Expected: `.po` files regenerated with the new translation strings. Do **not** run `compilemessages` yet — commit `.po` first so translators can fill in; then `make compilemessages` later.

- [ ] **Step 4: Extend the existing template tests**

In `src/notifications/tests/test_ticket_templates.py` add assertions for the three source variants — each rendering the correct sentence.

- [ ] **Step 5: Run tests**

Run: `uv run pytest src/notifications/tests/test_ticket_templates.py -v`
Expected: pass.

- [ ] **Step 6: Commit (ask user first)**

```bash
git add src/notifications/ src/events/
git status
```

Draft message:

```
feat(notifications): cancellation source + reason in ticket_cancelled (#370)

_build_ticket_updated_context now carries cancellation_source and
cancellation_reason. Templates branch the headline on the source:
user-initiated vs organizer vs stripe-dashboard. _send_refund_notifications
now reports payment.refund_amount (partial refunds) instead of the
original gross amount.
```

---

## Phase 11 — Recurring-events propagation check (light)

### Task 11.1: Ensure template tiers propagate the new fields to generated instances

**Files:**
- Read: `src/events/service/recurrence_service.py` (or wherever template-tier→instance-tier propagation happens)

- [ ] **Step 1: Grep for where template tier fields are copied to generated instance tiers**

Run: `grep -n "allow_user_cancellation\|tier_data\|template_tier" src/events/service/recurrence_service.py src/events/service/duplication.py`

If the propagation is field-by-field and enumerates tier attributes, add `allow_user_cancellation`, `cancellation_deadline_hours`, `refund_policy` to the list.

If the propagation uses `model_to_dict` or loops over `_meta.get_fields()`, no change is needed; the new fields are picked up automatically.

- [ ] **Step 2: Add a focused regression test**

`src/events/tests/test_service/test_recurrence_cancellation_propagation.py`:

```python
import pytest
from decimal import Decimal

pytestmark = pytest.mark.django_db


def test_generated_instance_inherits_cancellation_fields(
    recurring_template_event, run_generate_next_instance, tier_factory
):
    """Template-level allow_user_cancellation should appear on instance tiers."""
    tier = tier_factory(
        event=recurring_template_event,
        allow_user_cancellation=True,
        cancellation_deadline_hours=24,
        refund_policy={
            "tiers": [{"hours_before_event": 24, "refund_percentage": "50"}],
            "flat_fee": "0",
        },
    )
    instance_event = run_generate_next_instance(recurring_template_event)
    instance_tiers = list(instance_event.ticket_tiers.all())
    assert any(
        t.allow_user_cancellation is True
        and t.cancellation_deadline_hours == 24
        and t.refund_policy == tier.refund_policy
        for t in instance_tiers
    )
```

Reuse an existing fixture that covers template→instance generation (see `tests/test_service/test_recurrence_lifecycle.py` for the real fixture names).

- [ ] **Step 3: Run**

Run: `uv run pytest src/events/tests/test_service/test_recurrence_cancellation_propagation.py -v`
Expected: pass.

- [ ] **Step 4: Commit (ask user first)**

```bash
git add src/events/service/recurrence_service.py src/events/tests/test_service/test_recurrence_cancellation_propagation.py
git status
```

Draft message:

```
test(events): verify cancellation fields propagate to recurring events (#370)

Ensures allow_user_cancellation, cancellation_deadline_hours, and
refund_policy are copied from template tiers to generated instance
tiers (no code change needed if propagation is attribute-agnostic).
```

---

## Phase 12 — Full-suite sanity + OpenAPI regeneration

### Task 12.1: Run `make check` and full test suite

- [ ] **Step 1: Run quality gates**

Run: `make check`
Expected: format, lint, mypy, migrations, i18n, file-length all green.

- [ ] **Step 2: Run the full test suite**

Run: `make test`
Expected: green. If coverage for `events/service/cancellation_service.py` is < 90 %, add the missing test branches the diff identifies.

### Task 12.2: Regenerate OpenAPI spec

- [ ] **Step 1: Regenerate the spec**

The frontend consumes `.artifacts/openapi.json`. The repo's convention is to commit the regenerated file alongside backend API changes.

Run the repo's OpenAPI-export command (check `Makefile` / `justfile` for a target; if absent, the usual command is):

```bash
uv run python manage.py export_openapi_schema --api src.revel.urls:api --output .artifacts/openapi.json
```

If the exact command differs in this repo, find it via `rg -n "openapi" Makefile pyproject.toml`.

- [ ] **Step 2: Commit the regenerated spec (ask user first)**

```bash
git add .artifacts/openapi.json
git status
```

Draft message:

```
chore: regenerate OpenAPI spec for ticket cancellation endpoints (#370)
```

---

## Phase 13 — Open PR

### Task 13.1: Push and open the PR

- [ ] **Step 1: Verify branch & remote tracking**

Run: `git status && git log --oneline main..HEAD`
Confirm every phase's commits are present, the branch name is `feature/370-user-ticket-cancellation`.

- [ ] **Step 2: Ask the user for permission before pushing**

Show the list of commits and confirm the user wants to push / open PR.

- [ ] **Step 3: Push**

```bash
git push -u origin feature/370-user-ticket-cancellation
```

- [ ] **Step 4: Open PR via `gh pr create`**

Title: `feat(events): user-initiated ticket cancellation & refunds (#370)`

Body (via HEREDOC — `gh pr create --title ... --body "$(cat <<'EOF' ... EOF)"`):

```
## Summary
- Adds per-tier opt-in for user cancellation with a configurable refund
  policy (tiered % by hours-before-event + flat fee) snapshotted onto
  each ticket at purchase time.
- New endpoints: GET /events/tickets/{id}/cancellation-preview,
  POST /events/tickets/{id}/cancel (free, offline, at-the-door, online
  Stripe).
- Bundled fixes: refactored Stripe `charge.refunded` webhook to match
  refunds per-Payment (was cascading across a whole payment_intent);
  mark_ticket_refunded now fires TICKET_CANCELLED; admin cancel
  endpoints record audit fields; refund notifications report
  `payment.refund_amount` when partial.

Spec: docs/superpowers/specs/2026-04-23-user-ticket-cancellation-design.md

Closes #370

## Test plan
- [ ] make check passes
- [ ] make test passes (≥ 90 % coverage overall, 100 % on new service)
- [ ] Webhook matching: all 5 branches + duplicate-replay covered
- [ ] Stripe refund failure rolls back the transaction
- [ ] Admin cancel / mark-refunded audit fields populated + signal fires
- [ ] OpenAPI spec regenerated
- [ ] Manual: create tier with policy, buy ticket, cancel, confirm refund in Stripe dashboard
```

- [ ] **Step 5: Report the PR URL to the user.**

---

## Self-review

Before marking the plan done, I skimmed each spec section against the tasks:

| Spec § | Task(s) |
|---|---|
| 4.1 TicketTier fields | 2.1, 2.2 |
| 4.2 Ticket fields | 2.1, 2.2 |
| 4.3 Payment refund fields | 2.1, 2.2 |
| 4.4 RefundPolicy Pydantic | 1.1, 1.2, 7.1 |
| 4.5 Enums | 2.1 |
| 5 Refund calculation | 3.1, 3.2 |
| 6.1 Preview endpoint | 7.1, 7.2, 7.3 |
| 6.2 Cancel endpoint | 7.1, 7.2, 7.3 |
| 6.3 Tier admin endpoints accept new fields | 7.1, 8.1 |
| 6.4 Admin cancel backfill | 9.1 |
| 7.1 API-initiated flow | 5.1 |
| 7.2 Refund = 0 / offline / free | 5.1 tests |
| 7.3 Idempotency | 5.1, 7.3 |
| 7.4 Webhook refactor | 6.1, 6.2 |
| 7.5 Expired PENDING unchanged | (no task — we deliberately don't touch `handle_payment_intent_canceled`) |
| 8 Service layer split | 3.2, 5.1 |
| 9 Notifications | 10.1, 10.2 |
| 10 Guards (HTTP codes) | 7.2 |
| 11 Migration | 2.2 |
| 12 Bundled fixes | 6.2 (webhook), 10.1 (`payment.amount`), 9.1 (audit + `_original_ticket_status`) |
| 13 Batch snapshot propagation | 4.1, 4.2 |
| 14 Discount codes unchanged | (noted — no task) |
| 15 Recurring events propagation | 11.1 |
| 16 Testing plan | 1.1, 3.1, 5.1, 6.1, 7.3, 9.1, 10.1, 11.1 |

No placeholder steps. Type/field names consistent across tasks (`refund_policy_snapshot`, `cancellation_source`, `RefundQuote.refund_amount`, `Payment.RefundStatus`). All tasks produce self-contained commits.
