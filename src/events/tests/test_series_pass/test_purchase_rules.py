"""Purchase-rule comparison between a series pass and the tiers it covers (#1332)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from events.models import Event, MembershipTier, Organization, SeriesPass, SeriesPassTierLink, TicketTier
from events.utils.purchase_rules import tier_rule_stricter_than_pass

P = TicketTier.PurchasableBy

# pass rule -> tier rules that are acceptable (pass admits a subset of the tier)
_ALLOWED = {
    P.PUBLIC: {P.PUBLIC},
    P.MEMBERS: {P.PUBLIC, P.MEMBERS, P.INVITED_AND_MEMBERS},
    P.INVITED: {P.PUBLIC, P.INVITED, P.INVITED_AND_MEMBERS},
    P.INVITED_AND_MEMBERS: {P.PUBLIC, P.INVITED_AND_MEMBERS},
    P.NON_MEMBERS: {P.PUBLIC, P.NON_MEMBERS},
}


@pytest.mark.parametrize("pass_rule", list(P))
@pytest.mark.parametrize("tier_rule", list(P))
def test_rule_table(pass_rule: P, tier_rule: P) -> None:
    stricter = tier_rule_stricter_than_pass(tier_rule, pass_rule, tier_restricted_to_membership_tiers=False)
    assert stricter is (tier_rule not in _ALLOWED[pass_rule])


def test_membership_tier_restriction_is_always_stricter() -> None:
    assert tier_rule_stricter_than_pass(P.MEMBERS, P.PUBLIC, tier_restricted_to_membership_tiers=True)
    assert tier_rule_stricter_than_pass(P.MEMBERS, P.MEMBERS, tier_restricted_to_membership_tiers=True)


def test_unknown_pass_rule_fails_closed() -> None:
    assert tier_rule_stricter_than_pass("public", "bogus", tier_restricted_to_membership_tiers=False)


def _tier(event: Event, purchasable_by: P, **kw: object) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=f"T {purchasable_by}",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        purchasable_by=purchasable_by,
        **kw,
    )


@pytest.mark.django_db
class TestTierLinkPurchaseRules:
    @pytest.mark.parametrize("tier_rule", [P.MEMBERS, P.NON_MEMBERS])
    def test_public_pass_rejects_restricted_tier(self, series_pass: SeriesPass, event: Event, tier_rule: P) -> None:
        link = SeriesPassTierLink(series_pass=series_pass, event=event, tier=_tier(event, tier_rule))
        with pytest.raises(ValidationError) as exc:
            link.full_clean()
        assert "tier" in exc.value.message_dict

    def test_members_pass_rejects_membership_tier_restricted_tier(
        self, series_pass: SeriesPass, event: Event, organization: Organization
    ) -> None:
        series_pass.purchasable_by = P.MEMBERS
        series_pass.save()
        tier = _tier(event, P.MEMBERS)
        tier.restricted_to_membership_tiers.add(MembershipTier.objects.create(organization=organization, name="Gold"))
        link = SeriesPassTierLink(series_pass=series_pass, event=event, tier=tier)
        with pytest.raises(ValidationError):
            link.full_clean()

    def test_members_pass_accepts_members_tier(self, series_pass: SeriesPass, event: Event) -> None:
        series_pass.purchasable_by = P.MEMBERS
        series_pass.save()
        SeriesPassTierLink(series_pass=series_pass, event=event, tier=_tier(event, P.MEMBERS)).full_clean()

    def test_non_members_pass_accepts_non_members_tier_with_exclusions(
        self, series_pass: SeriesPass, event: Event, organization: Organization
    ) -> None:
        series_pass.purchasable_by = P.NON_MEMBERS
        series_pass.save()
        tier = _tier(event, P.NON_MEMBERS)
        tier.excluded_membership_tiers.add(MembershipTier.objects.create(organization=organization, name="Gold"))
        SeriesPassTierLink(series_pass=series_pass, event=event, tier=tier).full_clean()

    def test_public_pass_accepts_public_tier(self, series_pass: SeriesPass, event: Event) -> None:
        SeriesPassTierLink(series_pass=series_pass, event=event, tier=_tier(event, P.PUBLIC)).full_clean()

    def test_changing_pass_rule_over_stricter_link_rejected(self, series_pass: SeriesPass, event: Event) -> None:
        series_pass.purchasable_by = P.MEMBERS
        series_pass.save()
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=_tier(event, P.MEMBERS))
        series_pass.purchasable_by = P.PUBLIC
        with pytest.raises(ValidationError) as exc:
            series_pass.full_clean()
        assert "purchasable_by" in exc.value.message_dict


@pytest.mark.django_db
class TestExistingLinksUntouched:
    def test_resaving_a_pre_existing_mismatched_link_is_allowed(self, series_pass: SeriesPass, event: Event) -> None:
        """A link that predates the rule (bulk_create bypasses clean) can still be re-saved, e.g. by an admin inline."""
        tier = _tier(event, P.MEMBERS)
        SeriesPassTierLink.objects.bulk_create([SeriesPassTierLink(series_pass=series_pass, event=event, tier=tier)])
        link = SeriesPassTierLink.objects.get(series_pass=series_pass, event=event)
        link.save()  # full_cleans; must not raise

    def test_repointing_an_existing_link_to_a_stricter_tier_is_rejected(
        self, series_pass: SeriesPass, event: Event
    ) -> None:
        link = SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=_tier(event, P.PUBLIC))
        link.tier = _tier(event, P.MEMBERS)
        with pytest.raises(ValidationError) as exc:
            link.save()
        assert "tier" in exc.value.message_dict

    def test_moving_a_members_tier_link_to_a_public_pass_is_rejected(
        self, series_pass: SeriesPass, event: Event
    ) -> None:
        series_pass.purchasable_by = P.MEMBERS
        series_pass.save()
        link = SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=_tier(event, P.MEMBERS))
        public_pass = SeriesPass.objects.create(
            event_series=series_pass.event_series,
            name="Public pass",
            price=Decimal("36.00"),
            pro_rata_discount=Decimal("6.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=P.PUBLIC,
        )
        link.series_pass = public_pass
        with pytest.raises(ValidationError) as exc:
            link.save()
        assert "tier" in exc.value.message_dict

    def test_resaving_an_unchanged_link_on_a_members_pass_is_allowed(
        self, series_pass: SeriesPass, event: Event
    ) -> None:
        series_pass.purchasable_by = P.MEMBERS
        series_pass.save()
        link = SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=_tier(event, P.MEMBERS))
        link.save()


@pytest.mark.django_db
class TestDayMembershipGrantingTiers:
    """Granting tiers (#1334) may be non_members; only a non_members pass may cover one."""

    def _granting_tier(self, event: Event, organization: Organization) -> TicketTier:
        day = MembershipTier.objects.create(organization=organization, name="Day", kind=MembershipTier.Kind.TEMPORARY)
        return _tier(event, P.NON_MEMBERS, grants_membership_tier=day, max_tickets_per_user=1)

    def test_public_pass_rejects_non_members_granting_tier(
        self, series_pass: SeriesPass, event: Event, organization: Organization
    ) -> None:
        link = SeriesPassTierLink(series_pass=series_pass, event=event, tier=self._granting_tier(event, organization))
        with pytest.raises(ValidationError):
            link.full_clean()

    def test_non_members_pass_accepts_non_members_granting_tier(
        self, series_pass: SeriesPass, event: Event, organization: Organization
    ) -> None:
        series_pass.purchasable_by = P.NON_MEMBERS
        series_pass.save()
        SeriesPassTierLink(
            series_pass=series_pass, event=event, tier=self._granting_tier(event, organization)
        ).full_clean()
