"""Claiming an event token on an existing invitation: waivers are turned on, never off, and count as a use."""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventInvitation, Organization
from events.service.tokens import INVITATION_WAIVER_FIELDS, claim_invitation, create_event_token

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="tcw_owner")


@pytest.fixture
def invited_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="tcw_invited")


@pytest.fixture
def private_event(owner: RevelUser) -> Event:
    org = Organization.objects.create(name="TCW Org", slug="tcw-org", owner=owner)
    return Event.objects.create(
        organization=org,
        name="TCW Private Event",
        slug="tcw-private-event",
        visibility=Event.Visibility.PRIVATE,
        event_type=Event.EventType.PRIVATE,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + timedelta(days=7),
        end=timezone.now() + timedelta(days=8),
        requires_ticket=True,
    )


class TestTokenClaimWaivers:
    @pytest.mark.parametrize("field", INVITATION_WAIVER_FIELDS)
    def test_claim_turns_on_token_waiver_on_existing_invitation(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
        field: str,
    ) -> None:
        """A claim on an existing invitation turns on the token's waiver and uses up one token use."""
        invitation = EventInvitation.objects.create(event=private_event, user=invited_user, custom_message="keep")
        token = create_event_token(event=private_event, issuer=owner, duration=60, grants_invitation=True)
        token.invitation_payload = {field: True, "custom_message": "other"}
        token.save()

        claim_invitation(invited_user, token.pk)

        invitation.refresh_from_db()
        token.refresh_from_db()
        assert getattr(invitation, field) is True
        assert invitation.custom_message == "keep"
        assert token.uses == 1

    def test_repeat_claim_that_grants_nothing_is_free(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        EventInvitation.objects.create(event=private_event, user=invited_user, waives_purchase=True)
        token = create_event_token(event=private_event, issuer=owner, duration=60, grants_invitation=True)
        token.invitation_payload = {"waives_purchase": True}
        token.save()

        assert claim_invitation(invited_user, token.pk) is not None

        token.refresh_from_db()
        assert token.uses == 0

    def test_limited_waiver_token_upgrades_only_as_many_invitations_as_it_has_uses(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        other = django_user_model.objects.create_user(username="other@example.com", email="other@example.com")
        first = EventInvitation.objects.create(event=private_event, user=invited_user)
        second = EventInvitation.objects.create(event=private_event, user=other)
        token = create_event_token(event=private_event, issuer=owner, duration=60, grants_invitation=True, max_uses=1)
        token.invitation_payload = {"waives_purchase": True}
        token.save()

        assert claim_invitation(invited_user, token.pk) is not None
        assert claim_invitation(other, token.pk) == second  # used up: returned unchanged

        first.refresh_from_db()
        second.refresh_from_db()
        token.refresh_from_db()
        assert first.waives_purchase is True
        assert second.waives_purchase is False
        assert token.uses == 1

    def test_granting_a_waiver_updates_updated_at(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        invitation = EventInvitation.objects.create(event=private_event, user=invited_user)
        EventInvitation.objects.filter(pk=invitation.pk).update(updated_at=timezone.now() - timedelta(days=1))
        before = EventInvitation.objects.get(pk=invitation.pk).updated_at
        token = create_event_token(event=private_event, issuer=owner, duration=60, grants_invitation=True)
        token.invitation_payload = {"waives_questionnaire": True}
        token.save()

        claim_invitation(invited_user, token.pk)

        assert EventInvitation.objects.get(pk=invitation.pk).updated_at > before

    def test_guest_claim_turns_on_waivers_of_an_existing_invitation(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        from events.service.guest import claim_invitation_link

        invitation = EventInvitation.objects.create(event=private_event, user=invited_user)
        token = create_event_token(event=private_event, issuer=owner, duration=60, grants_invitation=True)
        token.invitation_payload = {"waives_questionnaire": True}
        token.save()

        claim_invitation_link(invited_user, private_event, token)

        invitation.refresh_from_db()
        assert invitation.waives_questionnaire is True

    def test_claim_never_turns_off_existing_waiver(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        """A waiver the invitation has stays on when the token does not grant it."""
        invitation = EventInvitation.objects.create(event=private_event, user=invited_user, waives_purchase=True)
        token = create_event_token(event=private_event, issuer=owner, duration=60, grants_invitation=True)
        token.invitation_payload = {"waives_questionnaire": True, "waives_purchase": False}
        token.save()

        claim_invitation(invited_user, token.pk)

        invitation.refresh_from_db()
        assert invitation.waives_purchase is True
        assert invitation.waives_questionnaire is True

    def test_adding_a_tier_to_an_existing_invitation_uses_up_a_use(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        """A single-use tier token adds its tier to one existing invitation only."""
        tier = private_event.ticket_tiers.create(name="VIP")
        other = django_user_model.objects.create_user(username="tier-other@example.com", email="tier-other@example.com")
        first = EventInvitation.objects.create(event=private_event, user=invited_user)
        second = EventInvitation.objects.create(event=private_event, user=other)
        token = create_event_token(
            event=private_event,
            issuer=owner,
            duration=60,
            grants_invitation=True,
            max_uses=1,
            ticket_tier_ids=[tier.pk],
        )

        assert claim_invitation(invited_user, token.pk) is not None
        assert claim_invitation(other, token.pk) == second  # used up: returned unchanged

        assert tier in first.tiers.all()
        assert tier not in second.tiers.all()
        token.refresh_from_db()
        assert token.uses == 1

    def test_reopening_a_used_up_link_returns_the_unchanged_invitation(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        token = create_event_token(event=private_event, issuer=owner, duration=60, grants_invitation=True, max_uses=1)
        token.invitation_payload = {"waives_questionnaire": True}
        token.save()

        first = claim_invitation(invited_user, token.pk)
        again = claim_invitation(invited_user, token.pk)

        assert first is not None and again is not None
        assert again.pk == first.pk
        token.refresh_from_db()
        assert token.uses == 1

    def test_only_a_true_payload_value_turns_a_waiver_on(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        invitation = EventInvitation.objects.create(event=private_event, user=invited_user)
        token = create_event_token(event=private_event, issuer=owner, duration=60, grants_invitation=True)
        token.invitation_payload = {"waives_purchase": "false"}
        token.save()

        claim_invitation(invited_user, token.pk)

        invitation.refresh_from_db()
        token.refresh_from_db()
        assert invitation.waives_purchase is False
        assert token.uses == 0

    def test_used_up_token_returns_the_holders_invitation_without_new_grants(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        invitation = EventInvitation.objects.create(event=private_event, user=invited_user)
        token = create_event_token(event=private_event, issuer=owner, duration=60, grants_invitation=True, max_uses=1)
        token.invitation_payload = {"waives_purchase": True}
        token.save()
        type(token).objects.filter(pk=token.pk).update(uses=1)

        result = claim_invitation(invited_user, token.pk)

        assert result is not None and result.pk == invitation.pk
        invitation.refresh_from_db()
        token.refresh_from_db()
        assert invitation.waives_purchase is False
        assert token.uses == 1
