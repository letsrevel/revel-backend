"""Tests for WaitlistOffer model."""

import datetime as dt
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, WaitlistOffer


@pytest.mark.django_db
class TestWaitlistOfferDefaults:
    def test_create_offer(self, event: Event, user: RevelUser) -> None:
        offer = WaitlistOffer.objects.create(
            event=event,
            user=user,
            expires_at=timezone.now() + dt.timedelta(hours=24),
            batch_id=uuid.uuid4(),
        )
        assert offer.status == WaitlistOffer.Status.PENDING
        assert offer.is_cutoff_batch is False
        assert offer.notified_at is None
        assert offer.claimed_at is None


@pytest.mark.django_db
class TestPendingUniqueConstraint:
    def test_two_pending_for_same_user_and_event_rejected(self, event: Event, user: RevelUser) -> None:
        WaitlistOffer.objects.create(
            event=event,
            user=user,
            expires_at=timezone.now() + dt.timedelta(hours=24),
            batch_id=uuid.uuid4(),
        )
        # full_clean (run from TimeStampedModel.save) surfaces the partial unique
        # constraint as ValidationError. If full_clean is bypassed (e.g. bulk_create
        # or QuerySet.update), the DB still raises IntegrityError.
        with pytest.raises((ValidationError, IntegrityError)):
            with transaction.atomic():
                WaitlistOffer.objects.create(
                    event=event,
                    user=user,
                    expires_at=timezone.now() + dt.timedelta(hours=24),
                    batch_id=uuid.uuid4(),
                )

    def test_non_pending_allows_new_pending(self, event: Event, user: RevelUser) -> None:
        old = WaitlistOffer.objects.create(
            event=event,
            user=user,
            expires_at=timezone.now() - dt.timedelta(hours=1),
            batch_id=uuid.uuid4(),
        )
        old.status = WaitlistOffer.Status.EXPIRED
        old.save(update_fields=["status"])

        # Should now be allowed
        WaitlistOffer.objects.create(
            event=event,
            user=user,
            expires_at=timezone.now() + dt.timedelta(hours=24),
            batch_id=uuid.uuid4(),
        )
        assert WaitlistOffer.objects.filter(event=event, user=user).count() == 2
