"""A 0% VAT tier of an invoicing organization needs a VAT exemption reason (EN 16931 category E)."""

from decimal import Decimal

import pytest
from ninja.errors import HttpError

from events.models import Event, Organization, TicketTier
from events.schema import TicketTierCreateSchema, TicketTierUpdateSchema
from events.service import ticket_service
from events.utils.vat_category import effective_vat_exemption_reason

pytestmark = pytest.mark.django_db

MESSAGE = "A VAT exemption reason is required for a ticket tier with a 0% VAT rate"


@pytest.fixture
def invoicing_org(event: Event) -> Organization:
    """The event's organization, invoicing automatically with a 20% default rate."""
    org = event.organization
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.vat_rate = Decimal("20")
    org.save()
    return org


def _create(event: Event, **kwargs: object) -> TicketTier:
    return ticket_service.create_ticket_tier(event, TicketTierCreateSchema(name="Zero", **kwargs))  # type: ignore[arg-type]


def test_zero_rate_tier_requires_reason_when_invoicing(event: Event, invoicing_org: Organization) -> None:
    with pytest.raises(HttpError) as exc_info:
        _create(event, vat_rate=Decimal("0"))
    assert exc_info.value.status_code == 422
    assert MESSAGE in str(exc_info.value)


def test_zero_rate_tier_accepts_tier_reason(event: Event, invoicing_org: Organization) -> None:
    tier = _create(event, vat_rate=Decimal("0"), vat_exemption_reason="§4 Nr. 20 UStG")
    assert tier.vat_exemption_reason == "§4 Nr. 20 UStG"


def test_zero_rate_tier_accepts_org_default_reason(event: Event, invoicing_org: Organization) -> None:
    invoicing_org.vat_exemption_reason = "Art. 132 VAT Directive"
    invoicing_org.save()
    tier = _create(event, vat_rate=Decimal("0"))
    assert tier.pk is not None


def test_org_zero_default_rate_tier_inherits_requirement(event: Event, invoicing_org: Organization) -> None:
    invoicing_org.vat_rate = Decimal("0")
    invoicing_org.save()
    with pytest.raises(HttpError) as exc_info:
        _create(event)
    assert exc_info.value.status_code == 422


def test_zero_rate_tier_without_reason_ok_when_not_invoicing(event: Event, invoicing_org: Organization) -> None:
    invoicing_org.invoicing_mode = Organization.InvoicingMode.NONE
    invoicing_org.save()
    assert _create(event, vat_rate=Decimal("0")).pk is not None


def test_update_to_zero_rate_requires_reason(event: Event, invoicing_org: Organization) -> None:
    tier = _create(event, vat_rate=Decimal("10"))
    with pytest.raises(HttpError) as exc_info:
        ticket_service.update_ticket_tier(
            tier,
            TicketTierUpdateSchema(vat_rate=Decimal("0")),  # type: ignore[call-arg]
        )
    assert exc_info.value.status_code == 422
    assert MESSAGE in str(exc_info.value)

    updated = ticket_service.update_ticket_tier(
        tier,
        TicketTierUpdateSchema(vat_rate=Decimal("0"), vat_exemption_reason="Exempt"),  # type: ignore[call-arg]
    )
    assert updated.vat_exemption_reason == "Exempt"


def test_effective_reason_prefers_tier(event: Event, invoicing_org: Organization) -> None:
    invoicing_org.vat_exemption_reason = " org reason "
    tier = TicketTier(event=event, vat_exemption_reason=" tier reason ")
    assert effective_vat_exemption_reason(tier, invoicing_org) == "tier reason"
    tier.vat_exemption_reason = "  "
    assert effective_vat_exemption_reason(tier, invoicing_org) == "org reason"
    invoicing_org.vat_exemption_reason = ""
    assert effective_vat_exemption_reason(tier, invoicing_org) == ""
