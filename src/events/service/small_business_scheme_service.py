"""Attendee invoicing for small-business-scheme organizers without an EU VAT ID (#1244).

Group A only: DE *Kleinunternehmer* (§ 19 UStG) and AT *Kleinunternehmer* (§ 6 Abs. 1 Z 27 UStG).
VAT-charging organizers that hold only a tax number (group B) still need an EU VAT ID; see
``docs/compliance/eu/de.md``. Legal references live in :mod:`events.utils.small_business`.

Split from ``attendee_invoice_service`` (1000-line cap). Models and utils only.
"""

import typing as t

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from common.constants import EU_MEMBER_STATES
from events.compliance import BuyerContext, Decision
from events.compliance.base import SMALL_BUSINESS_VIRTUAL_B2B, country_name
from events.compliance.registry import resolve_org_country
from events.models.event import Event
from events.models.organization import Organization
from events.models.ticket import TicketTier
from events.utils.small_business import (
    SmallBusinessScheme,
    is_small_business,
    scheme_country,
    scheme_exemption_reason,
    tax_number_format_known,
    tax_number_is_valid,
    tax_number_label,
)

# The refusal recorded on the SkippedFiscalDocument (#1091) when a scheme organizer sells a virtual
# event to a business in another EU country: the supply is taxed where the buyer is (Art. 44 /
# Art. 196 of Directive 2006/112/EC), which takes a seller VAT ID and a reverse-charge invoice
# (EN 16931 BR-AE-02/03) the scheme does not provide. Invoicing is not offered rather than silently
# charging domestic treatment.
VIRTUAL_B2B_BLOCKED_MESSAGE = _(
    "Revel can't invoice a business customer in {country} for a virtual event on behalf of an organizer under a "
    "small-business scheme: that sale is taxed where the customer is and needs a VAT ID and a reverse-charge "
    "invoice. Issue this one from your own invoicing software."
)


def _scheme_country_matches(org: Organization) -> bool:
    """Whether the organizer is established where its declared scheme applies (``de_19`` ⇒ DE, ``at_6_1_27`` ⇒ AT)."""
    return resolve_org_country(org) == scheme_country(org.small_business_scheme)


def scheme_prerequisite_errors(org: Organization) -> list[str]:
    """Why a scheme organizer cannot invoice attendees yet: translated messages, empty when it can.

    Stands in for the VIES-validated VAT ID (``validate_invoicing_prerequisites``): the scheme must
    match the country of establishment (which doubles as the DE/AT allowlist) and the tax registration
    number (BT-32; DE § 34a S. 1 Nr. 2 UStDV) must be format-valid for that country.
    """
    errors: list[str] = []
    country = scheme_country(org.small_business_scheme)
    if not _scheme_country_matches(org):
        errors.append(
            str(_("The small-business scheme you declared applies to organizations established in {country}")).format(
                country=country_name(country)
            )
        )
    if not tax_number_is_valid(country, org.tax_registration_number):
        errors.append(
            str(_("A valid tax registration number ({label}) is required under the small-business scheme")).format(
                label=tax_number_label(country)
            )
        )
    return errors


def seller_identity_missing_fields(org: Organization) -> list[str]:
    """Organization field names still missing for the seller to be identified on an e-invoice.

    Returns:
        ``[]`` with a VIES-validated VAT ID; otherwise ``["vat_id"]`` for an organizer without a
        scheme, and for a scheme organizer ``"small_business_scheme"`` (declared for the wrong
        country) and/or ``"tax_registration_number"`` (missing or malformed).
    """
    if org.vat_id_validated:
        return []
    if not is_small_business(org):
        return ["vat_id"]
    missing: list[str] = []
    if not _scheme_country_matches(org):
        missing.append("small_business_scheme")
    if not tax_number_is_valid(scheme_country(org.small_business_scheme), org.tax_registration_number):
        missing.append("tax_registration_number")
    return missing


def _live_tiers_charging_vat(org: Organization) -> bool:
    """Whether a tier of an event that has not ended still carries a positive VAT rate of its own."""
    return (
        TicketTier.objects.filter(event__organization=org, event__end__gte=timezone.now(), vat_rate__gt=0)
        .exclude(event__status=Event.EventStatus.CANCELLED)
        .exists()
    )


def prepare_billing_update(org: Organization, update_data: dict[str, t.Any]) -> dict[str, t.Any]:
    """Validate a billing-info PATCH against the small-business rules and prefill the exemption reason.

    A declared scheme means no VAT is ever charged: the organization's default rate must be 0% and
    no live tier may override it upwards (a Kleinunternehmer showing VAT owes it, DE § 14c UStG /
    AT § 11 Abs. 12 UStG). A tax registration number is format-checked whenever one is given and
    the country's format is known; it only becomes mandatory when invoicing is turned on.

    Returns:
        ``update_data``, plus the scheme's statutory exemption note as ``vat_exemption_reason`` when a
        scheme is declared and no reason is set (the organizer may edit it afterwards).

    Raises:
        HttpError 422: On a positive default VAT rate or VAT-charging live tier under a scheme, or a
            malformed tax registration number.
    """
    scheme = update_data.get("small_business_scheme", org.small_business_scheme)
    number = str(update_data.get("tax_registration_number", org.tax_registration_number) or "").strip()
    country = scheme_country(scheme) or resolve_org_country(org)
    if number and tax_number_format_known(country) and not tax_number_is_valid(country, number):
        raise HttpError(
            422,
            str(_("That doesn't look like a valid {label} for {country}.")).format(
                label=tax_number_label(country), country=country_name(country)
            ),
        )
    if scheme == SmallBusinessScheme.NONE:
        return update_data
    vat_rate = update_data.get("vat_rate")
    if (org.vat_rate if vat_rate is None else vat_rate) > 0:
        raise HttpError(
            422, str(_("An organizer under a small-business scheme charges no VAT: set the default VAT rate to 0%."))
        )
    if _live_tiers_charging_vat(org):
        raise HttpError(
            422,
            str(_("Some ticket tiers still charge VAT. Set them to 0% before declaring a small-business scheme.")),
        )
    reason = str(update_data.get("vat_exemption_reason", org.vat_exemption_reason) or "")
    if not reason.strip():
        return {**update_data, "vat_exemption_reason": scheme_exemption_reason(scheme)}
    return update_data


def assert_tier_rate_allowed(tier: TicketTier, org: Organization) -> None:
    """Refuse a tier that charges VAT for an organizer under a small-business scheme.

    Raises:
        HttpError 422: If the organizer has a scheme and the tier carries a positive VAT rate.
    """
    if is_small_business(org) and tier.vat_rate is not None and tier.vat_rate > 0:
        raise HttpError(
            422, str(_("An organizer under a small-business scheme charges no VAT: the tier's VAT rate must be 0%."))
        )


def virtual_b2b_decision(org: Organization, event: Event, buyer: BuyerContext) -> Decision:
    """Whether Revel may invoice this sale for a scheme organizer; always allowed for any other organizer.

    Refused when a scheme organizer sold a *virtual* event to a business established in another
    EU country (see :data:`VIRTUAL_B2B_BLOCKED_MESSAGE`). Physical admission is taxed at the venue
    (Art. 53), consumers and non-EU buyers never need a reverse-charge invoice, so those stay allowed.
    """
    if not is_small_business(org) or not event.is_virtual or not buyer.vat_country:
        return Decision.allow()
    seller_country = resolve_org_country(org)
    if buyer.vat_country == seller_country or buyer.vat_country not in EU_MEMBER_STATES:
        return Decision.allow()
    return Decision.block(
        str(VIRTUAL_B2B_BLOCKED_MESSAGE).format(country=country_name(buyer.vat_country)),
        code=SMALL_BUSINESS_VIRTUAL_B2B,
        country=seller_country,
    )
