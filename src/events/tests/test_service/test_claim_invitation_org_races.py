"""Race-protection behavior for organization token claims.

``claim_invitation`` creates ``OrganizationMember``/``OrganizationStaff`` rows;
both carry a unique constraint on (organization, user) and inherit
``TimeStampedModel`` (whose ``save`` runs ``full_clean``), so a lost creation
race surfaces as ``ValidationError`` rather than ``IntegrityError``. These
tests pin that a concurrently-committed row is absorbed by the
race-protection helper instead of escaping as a 500.
"""

import typing as t
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.db.models import Manager

from accounts.models import RevelUser
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    OrganizationToken,
)
from events.service import organization_service


def _membership_token(organization: Organization, issuer: RevelUser) -> OrganizationToken:
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    return OrganizationToken.objects.create(organization=organization, issuer=issuer, membership_tier=default_tier)


@contextmanager
def _force_first_lookup_miss(manager: Manager[t.Any]) -> t.Iterator[None]:
    """Make ``manager.filter(...)`` miss on its first call, then behave normally.

    Simulates the concurrent double-claim: the racing row is already committed
    by the time our INSERT lands, but our own lookup ran too early to see it.
    """
    call_count = 0
    real_filter = manager.filter

    def fake_filter(*args: t.Any, **kwargs: t.Any) -> t.Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return manager.none()
        return real_filter(*args, **kwargs)

    with patch.object(manager, "filter", side_effect=fake_filter):
        yield


@pytest.mark.django_db
class TestClaimInvitationLostRaces:
    def test_membership_claim_lost_race_is_absorbed(
        self, organization: Organization, nonmember_user: RevelUser, organization_owner_user: RevelUser
    ) -> None:
        """A concurrently-committed member row must not escape as ValidationError."""
        token = _membership_token(organization, organization_owner_user)
        OrganizationMember.objects.create(organization=organization, user=nonmember_user)

        with _force_first_lookup_miss(OrganizationMember.objects):
            result = organization_service.claim_invitation(nonmember_user, token.id)

        assert result is None  # racing claim already granted membership
        assert OrganizationMember.objects.filter(organization=organization, user=nonmember_user).count() == 1
        token.refresh_from_db()
        assert token.uses == 0

    def test_staff_claim_lost_race_is_absorbed(
        self, organization: Organization, nonmember_user: RevelUser, organization_owner_user: RevelUser
    ) -> None:
        """A concurrently-committed staff row must not escape as ValidationError."""
        token = OrganizationToken.objects.create(
            organization=organization, issuer=organization_owner_user, grants_staff_status=True, grants_membership=False
        )
        OrganizationStaff.objects.create(organization=organization, user=nonmember_user)

        with _force_first_lookup_miss(OrganizationStaff.objects):
            result = organization_service.claim_invitation(nonmember_user, token.id)

        assert result is None
        assert OrganizationStaff.objects.filter(organization=organization, user=nonmember_user).count() == 1

    def test_clean_claim_still_grants_membership(
        self, organization: Organization, nonmember_user: RevelUser, organization_owner_user: RevelUser
    ) -> None:
        """The conversion must not change the happy path."""
        token = _membership_token(organization, organization_owner_user)

        result = organization_service.claim_invitation(nonmember_user, token.id)

        assert result == organization
        assert OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()
        token.refresh_from_db()
        assert token.uses == 1
