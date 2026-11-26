"""A tier that a series pass covers cannot get a stricter purchase rule after the link (#1341).

#1332 checks the link and the pass rule. The pass issues tickets in the tier to every holder, so a stricter rule on
the tier afterwards would let the pass bypass it.
"""

import pytest
from django.core.exceptions import ValidationError

from events.models import MembershipTier, Organization, SeriesPass, TicketTier
from events.schema import TicketTierUpdateSchema
from events.service import ticket_service
from events.tests.test_series_pass.covered_events import cover

pytestmark = pytest.mark.django_db

P = TicketTier.PurchasableBy


def _update(tier: TicketTier, **fields: object) -> TicketTier:
    return ticket_service.update_ticket_tier(tier, TicketTierUpdateSchema(**fields))  # type: ignore[arg-type]


def _members_pass_and_tier(series_pass: SeriesPass) -> TicketTier:
    SeriesPass.objects.filter(pk=series_pass.pk).update(purchasable_by=P.MEMBERS)
    _, tier = cover(series_pass, "members", days=3)
    TicketTier.objects.filter(pk=tier.pk).update(purchasable_by=P.MEMBERS)
    return TicketTier.objects.get(pk=tier.pk)


@pytest.mark.parametrize("stricter", [P.MEMBERS, P.INVITED, P.NON_MEMBERS])
def test_a_stricter_rule_on_a_covered_tier_is_refused(series_pass: SeriesPass, stricter: P) -> None:
    _, tier = cover(series_pass, "public", days=3)  # a public pass covers a public tier

    with pytest.raises(ValidationError) as exc:
        _update(tier, purchasable_by=stricter)

    assert "purchasable_by" in exc.value.message_dict
    assert TicketTier.objects.get(pk=tier.pk).purchasable_by == P.PUBLIC


def test_a_new_membership_tier_restriction_on_a_covered_tier_is_refused(
    series_pass: SeriesPass, organization: Organization
) -> None:
    tier = _members_pass_and_tier(series_pass)
    gold = MembershipTier.objects.create(organization=organization, name="Gold")

    with pytest.raises(ValidationError) as exc:
        _update(tier, restricted_to_membership_tiers_ids=[gold.pk])

    assert "purchasable_by" in exc.value.message_dict


def test_a_looser_rule_on_a_covered_tier_is_allowed(series_pass: SeriesPass) -> None:
    tier = _members_pass_and_tier(series_pass)

    assert _update(tier, purchasable_by=P.PUBLIC).purchasable_by == P.PUBLIC


def test_a_stricter_rule_on_a_tier_that_no_pass_covers_is_allowed(
    series_pass: SeriesPass, organization: Organization
) -> None:
    _, tier = cover(series_pass, "public", days=3)
    tier.series_pass_links.all().delete()

    assert _update(tier, purchasable_by=P.MEMBERS).purchasable_by == P.MEMBERS


def test_a_link_that_predates_the_rule_does_not_block_other_edits(
    series_pass: SeriesPass, organization: Organization
) -> None:
    """A tier already stricter than its pass (a link from before #1332) stays editable; the form resends its rule."""
    _, tier = cover(series_pass, "legacy", days=3)
    gold = MembershipTier.objects.create(organization=organization, name="Gold")
    TicketTier.objects.filter(pk=tier.pk).update(purchasable_by=P.MEMBERS)
    tier.restricted_to_membership_tiers.add(gold)
    tier = TicketTier.objects.get(pk=tier.pk)

    renamed = _update(tier, name="Renamed", purchasable_by=P.MEMBERS, restricted_to_membership_tiers_ids=[gold.pk])

    assert renamed.name == "Renamed"
