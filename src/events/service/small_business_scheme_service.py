"""Attendee invoicing for small-business-scheme organizers (#1244).

Group A only: DE *Kleinunternehmer* (§ 19 UStG) and AT *Kleinunternehmer* (§ 6 Abs. 1 Z 27 UStG).
An AT organizer is identified by its Steuernummer; a DE organizer must hold a VIES-validated USt-IdNr.
(#1258: Revel's platform fee to it is reverse-charged). VAT-charging organizers that hold only a tax
number (group B) still need an EU VAT ID; see ``docs/compliance/eu/de.md``. Legal references live in
:mod:`events.utils.small_business`.

Split from ``attendee_invoice_service`` (1000-line cap). Models and utils only.
"""

import copy
import typing as t
from decimal import Decimal, InvalidOperation

from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from common.country import normalize_country_code
from events.compliance import Decision
from events.compliance.base import SELLER_IDENTITY_MISSING, country_name
from events.compliance.registry import resolve_org_country
from events.models.attendee_invoice import AttendeeInvoice, BuyerBillingSnapshot
from events.models.event import Event
from events.models.organization import Organization
from events.models.ticket import TicketTier
from events.utils.small_business import (
    SMALL_BUSINESS_FOREIGN_VENUE,
    SmallBusinessScheme,
    is_small_business,
    scheme_country,
    scheme_exemption_reason,
    scheme_identity_complete,
    scheme_matches_country,
    scheme_requires_vat_id,
    scheme_sale_block,
    tax_number_format_known,
    tax_number_is_valid,
    tax_number_label,
)

# Refusals recorded on the SkippedFiscalDocument (#1091). A scheme covers domestic supplies only, so a
# sale whose place of supply lies outside the scheme's country is not invoiced: admission to a physical
# event is taxed where it takes place (Art. 53/54 of Directive 2006/112/EC), virtual attendance where
# the customer is (Art. 44; Art. 54(1) as amended by Directive (EU) 2022/542), and a business customer
# would moreover need a reverse-charge invoice with a seller VAT ID (EN 16931 BR-AE-02/03).
FOREIGN_VENUE_BLOCKED_MESSAGE = _(
    "Revel can't invoice admission to an event held in {country} on behalf of an organizer under the {home} "
    "small-business scheme: admission is taxed where the event takes place, outside the scheme. Issue this one "
    "from your own invoicing software."
)
VIRTUAL_CROSS_BORDER_BLOCKED_MESSAGE = _(
    "Revel can't invoice a customer in {country} for a virtual event on behalf of an organizer under the {home} "
    "small-business scheme: that sale is taxed where the customer is, outside the scheme. Issue this one from "
    "your own invoicing software."
)
SELLER_IDENTITY_MISSING_MESSAGE = _(
    "Revel can't issue this invoice: the organization's tax identity is incomplete. Add a VIES-validated VAT ID, "
    "or complete the small-business scheme declaration, and issue this one from your own invoicing software."
)
INVOICING_WOULD_STOP_MESSAGE = _("This change would stop attendee invoicing: {details}. Turn invoicing off first.")
# #1258: Revel's fee to a German Kleinunternehmer is reverse-charged (see ``events.utils.small_business``).
VAT_ID_REQUIRED_MESSAGE = _(
    "The German small-business scheme (§ 19 UStG) requires a VIES-validated USt-IdNr.: Revel's platform fee is "
    "reverse-charged to you, which needs it. The USt-IdNr. is free from the Bundeszentralamt für Steuern (BZSt)"
)


def _scheme_country_error(org: Organization) -> str:
    """The translated refusal when the declared scheme is not the one of the country of establishment, else ``""``."""
    expected = scheme_country(org.small_business_scheme)
    if not expected or scheme_matches_country(org.small_business_scheme, resolve_org_country(org)):
        return ""
    return str(_("The small-business scheme you declared applies to organizations established in {country}")).format(
        country=country_name(expected)
    )


def _tax_number_error(org: Organization) -> str:
    """The translated refusal when a scheme organizer's tax registration number is missing or malformed."""
    country = scheme_country(org.small_business_scheme)
    if tax_number_is_valid(country, org.tax_registration_number):
        return ""
    return str(_("A valid tax registration number ({label}) is required under the small-business scheme")).format(
        label=tax_number_label(country)
    )


def _scheme_identifier_error(org: Organization) -> str:
    """What a scheme organizer without a validated VAT ID still lacks: the VAT ID itself (DE, #1258) or a tax number."""
    if scheme_requires_vat_id(org.small_business_scheme):
        return str(VAT_ID_REQUIRED_MESSAGE)
    return _tax_number_error(org)


def seller_identity_errors(org: Organization) -> list[str]:
    """Why the seller cannot be identified on an attendee invoice: translated messages, empty when it can.

    A VIES-validated VAT ID identifies the seller (BT-31); a declared scheme must still be the one of
    the country of establishment. Without a validated VAT ID, a scheme organizer needs the scheme to
    match its country (which doubles as the DE/AT allowlist) and then what the scheme accepts: a DE
    Kleinunternehmer needs the VAT ID itself (#1258), an AT one a format-valid tax registration number
    (BT-32); anyone else needs the VAT ID.
    """
    if org.vat_id_validated:
        return [error for error in (_scheme_country_error(org),) if error]
    if not is_small_business(org):
        return [str(_("VAT ID must be validated via VIES"))]
    if scheme_identity_complete(
        org.small_business_scheme, org.vat_id_validated, resolve_org_country(org), org.tax_registration_number
    ):
        return []
    return [error for error in (_scheme_country_error(org), _scheme_identifier_error(org)) if error]


def scheme_path_identity_errors(org: Organization, candidate: Organization | None = None) -> list[str]:
    """The identity problems this feature introduced, for the running guards.

    An organizer is off the guard only with a VIES-validated VAT ID, or when neither its current
    state nor ``candidate`` (the post-PATCH state, when checking an update) is on the scheme path:
    that is the VIES organizer whose validation lapsed, which keeps today's behaviour (invoices are
    still issued). A scheme organizer without a validated VAT ID stays guarded whatever VAT ID string
    it stores (VIES may have rejected it), and so does a PATCH that drops its scheme.
    """
    candidate = candidate or org
    # A declared scheme must match the country of establishment, validated VAT ID or not.
    if is_small_business(candidate) and (country_error := _scheme_country_error(candidate)):
        return [country_error]
    # The candidate's validation state: a VAT-ID delete or reset is exactly the change that removes it.
    if candidate.vat_id_validated:
        return []
    if not is_small_business(org) and not is_small_business(candidate):
        return []
    return seller_identity_errors(candidate)


def seller_identity_missing_fields(org: Organization) -> list[str]:
    """Organization field names still missing for the seller to be identified on an e-invoice.

    Returns:
        ``[]`` with a VIES-validated VAT ID; otherwise ``["vat_id"]`` for an organizer without a
        scheme or under one that requires the VAT ID (DE, #1258), and for a scheme organizer
        ``"small_business_scheme"`` (declared for the wrong country) and/or
        ``"tax_registration_number"`` (missing or malformed, AT).
    """
    if org.vat_id_validated:
        return []
    if not is_small_business(org):
        return ["vat_id"]
    missing: list[str] = []
    if _scheme_country_error(org):
        missing.append("small_business_scheme")
    if scheme_requires_vat_id(org.small_business_scheme):
        missing.append("vat_id")
    elif _tax_number_error(org):
        missing.append("tax_registration_number")
    return missing


def live_tiers_charging_vat(org: Organization) -> QuerySet[TicketTier]:
    """Tiers of ticketed events that have not ended that still carry a positive VAT rate of their own.

    RSVP-only events (``requires_ticket=False``) sell nothing, so their tiers are left out.

    The one source for both the declaration guard and the ``tiers_charging_vat`` billing-info field.
    """
    return (
        TicketTier.objects.filter(
            event__organization=org, event__requires_ticket=True, event__end__gte=timezone.now(), vat_rate__gt=0
        )
        .exclude(event__status=Event.EventStatus.CANCELLED)
        .select_related("event")
        .prefetch_related(None)  # the default manager prefetches membership tiers; not needed here
        .order_by("event__start", "name", "pk")
    )


def _exemption_reason_after(org: Organization, candidate: Organization) -> str:
    """The organization default exemption reason after a scheme change.

    The statutory note is prefilled when a scheme is declared and no reason is set, replaced when the
    scheme changes and the reason still is the old note, and cleared when the scheme is dropped and
    the reason still is its note. Wording of the organizer's own is kept as it is: invoices of a scheme
    organizer always carry the statutory note as well (``attendee_invoice_lines._exempt_reasons``).
    """
    reason = str(candidate.vat_exemption_reason or "").strip()
    if candidate.small_business_scheme == org.small_business_scheme:
        return reason
    if reason == scheme_exemption_reason(org.small_business_scheme) or not reason:
        return scheme_exemption_reason(candidate.small_business_scheme)
    return reason


def _assert_scheme_declaration_allowed(org: Organization, candidate: Organization) -> None:
    """The rules for declaring or keeping a scheme on a billing PATCH (``candidate`` is the post-PATCH state).

    Raises:
        HttpError 422: A DE scheme without a VIES-validated USt-IdNr. (#1258); a positive default rate; or,
            when declaring (none -> scheme), a live tier that still charges VAT.
    """
    if scheme_requires_vat_id(candidate.small_business_scheme) and not candidate.vat_id_validated:
        raise HttpError(422, str(VAT_ID_REQUIRED_MESSAGE))
    if candidate.vat_rate > 0:
        raise HttpError(
            422, str(_("An organizer under a small-business scheme charges no VAT: set the default VAT rate to 0%."))
        )
    # Only when declaring (none -> scheme): afterwards any tier rate is ignored at the effective-rate
    # choke point, so a copied rate must not block unrelated billing edits.
    if org.small_business_scheme == SmallBusinessScheme.NONE and live_tiers_charging_vat(org).exists():
        raise HttpError(
            422, str(_("Some ticket tiers still charge VAT. Set them to 0% before declaring a small-business scheme."))
        )


def prepare_billing_update(org: Organization, update_data: dict[str, t.Any]) -> dict[str, t.Any]:
    """Validate a billing-info PATCH against the small-business rules and keep the exemption note in step.

    Checked on the post-PATCH state: a declared scheme must be the one of the country of establishment
    (a VIES-validated organizer included); a tax registration number, whenever one is given and the
    country's format is known, must be format-valid for that country; a scheme that requires the VAT ID
    (DE, #1258) is declared or kept only with a VIES-validated one, whatever the invoicing mode, since
    the platform fee's reverse charge hinges on it; a declared scheme means no VAT is ever charged, so
    the default rate must be 0% and no live tier may override it upwards (a Kleinunternehmer showing
    VAT owes it, DE § 14c UStG / AT § 11 Abs. 12 UStG); and while attendee invoicing is on, no change
    may leave the seller unidentifiable (:func:`seller_identity_errors`).

    Returns:
        ``update_data``, with ``vat_exemption_reason`` adjusted as :func:`_exemption_reason_after` says.

    Raises:
        HttpError 422: On any of the refusals above.
    """
    candidate = copy.copy(org)
    for field, value in update_data.items():
        if value is not None:
            setattr(candidate, field, value)
    scheme = candidate.small_business_scheme
    if error := _scheme_country_error(candidate):  # before the number: its format depends on the country
        raise HttpError(422, error)
    number = str(candidate.tax_registration_number or "").strip()
    country = scheme_country(scheme) or resolve_org_country(candidate)
    if number and tax_number_format_known(country) and not tax_number_is_valid(country, number):
        raise HttpError(
            422,
            str(_("That doesn't look like a valid {label} for {country}.")).format(
                label=tax_number_label(country), country=country_name(country)
            ),
        )
    if scheme != SmallBusinessScheme.NONE:
        _assert_scheme_declaration_allowed(org, candidate)
    if org.invoicing_mode != Organization.InvoicingMode.NONE and (
        errors := scheme_path_identity_errors(org, candidate)
    ):
        raise HttpError(422, str(INVOICING_WOULD_STOP_MESSAGE).format(details="; ".join(errors)))
    reason = _exemption_reason_after(org, candidate)
    if reason != str(candidate.vat_exemption_reason or ""):
        return {**update_data, "vat_exemption_reason": reason}
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


def assert_draft_lines_exempt(org: Organization | None, line_items: t.Iterable[t.Mapping[str, t.Any]]) -> None:
    """Refuse a draft edit that would make a scheme organizer's invoice show VAT.

    Raises:
        HttpError 422: If the organizer has a scheme and any submitted line carries a VAT rate or amount.
    """
    if org is None or not is_small_business(org):
        return
    try:
        shows_vat = any(
            Decimal(str(item.get("vat_rate") or 0)) != 0 or Decimal(str(item.get("vat_amount") or 0)) != 0
            for item in line_items
        )
    except InvalidOperation as exc:
        # The API's schema already bounds what reaches here; a raw row is still a 422, never a 500.
        raise HttpError(422, str(_("Invalid line amounts: {details}")).format(details="vat_rate / vat_amount")) from exc
    if shows_vat:
        raise HttpError(
            422, str(_("An organizer under a small-business scheme charges no VAT: set the VAT of every line to 0."))
        )


def vat_id_change_refusal(org: Organization, new_vat_id: str, *, validated: bool) -> str:
    """Why a VAT-ID change (set, or clear with ``""``) is refused for a scheme organizer, or ``""``.

    The candidate takes the new VAT ID and its country (the prefix, ``EL`` read as ``GR``; cleared on
    delete) and the given VIES outcome. A declared scheme must still be the one of that country (a DE
    scheme cannot hold an AT VAT ID, as the billing PATCH refuses too). A scheme that requires the VAT
    ID (DE, #1258) must keep a validated one whatever the invoicing mode: the platform fee's reverse
    charge hinges on it. Otherwise, while invoicing is on, the change may not leave the seller
    unidentifiable (:func:`scheme_path_identity_errors`): an AT organizer's tax number stands in.
    """
    candidate = copy.copy(org)
    candidate.vat_id = new_vat_id
    candidate.vat_country_code = normalize_country_code(new_vat_id[:2]) if new_vat_id[:2].isalpha() else ""
    candidate.vat_id_validated = validated and bool(new_vat_id)
    if is_small_business(candidate) and (error := _scheme_country_error(candidate)):
        return error
    if scheme_requires_vat_id(candidate.small_business_scheme) and not candidate.vat_id_validated:
        return str(VAT_ID_REQUIRED_MESSAGE)
    if org.invoicing_mode == Organization.InvoicingMode.NONE:
        return ""
    if errors := scheme_path_identity_errors(org, candidate):
        return str(INVOICING_WOULD_STOP_MESSAGE).format(details="; ".join(errors))
    return ""


def assert_vat_id_change_allowed(org: Organization, new_vat_id: str) -> None:
    """Refuse a VAT-ID change judged on the pessimistic VIES outcome (the right call for a delete).

    A set is decided on the actual outcome instead (``events.service.vies_service.set_org_vat_id``), so
    a DE organizer whose validation lapsed can recover by resubmitting a valid USt-IdNr. (#1258).

    Raises:
        HttpError 422: :func:`vat_id_change_refusal` is non-empty.
    """
    if refusal := vat_id_change_refusal(org, new_vat_id, validated=False):
        raise HttpError(422, refusal)


class SellerIdentitySnapshot(t.TypedDict):
    """The seller identifier fields an :class:`AttendeeInvoice` snapshots from its organization."""

    seller_vat_id: str
    seller_tax_registration_number: str
    seller_vat_country: str


def seller_identity_snapshot(
    org: Organization, event: Event | None, *, stored_vat_country: str = ""
) -> SellerIdentitySnapshot:
    """The seller identifiers as generation snapshots them; draft issue reuses the rule when the scheme changed.

    The stored VAT ID is copied as before, except on the scheme path (#1244): a scheme organizer without
    a validated VAT ID is identified by its tax number (BT-32), never by a typed-in BT-31. Physical
    admission is taxed where the event takes place, so the event's VAT country drives the invoice
    country; a virtual event is supplied from the organizer's establishment (#869). Without an event
    (deleted since the sale) there is nothing to derive it from, and ``stored_vat_country`` is kept.
    """
    hide_vat_id = is_small_business(org) and not org.vat_id_validated
    if event is None:
        country = stored_vat_country
    else:
        country = resolve_org_country(org) if event.is_virtual else event.effective_vat_country
    return SellerIdentitySnapshot(
        seller_vat_id="" if hide_vat_id else org.vat_id,
        seller_tax_registration_number=org.tax_registration_number,
        seller_vat_country=country,
    )


def seller_identity_decision(org: Organization) -> Decision:
    """Defense in depth at generation for the scheme path only (:func:`scheme_path_identity_errors`)."""
    if not scheme_path_identity_errors(org):
        return Decision.allow()
    return Decision.block(
        str(SELLER_IDENTITY_MISSING_MESSAGE), code=SELLER_IDENTITY_MISSING, country=resolve_org_country(org)
    )


def buyer_country(vat_country: str | None, billing_country: str | None) -> str:
    """The buyer's country for the scheme gates: the VAT country first, else the structured billing address's.

    One order for generation (from the checkout snapshot) and for issuing a draft (from the invoice).
    """
    return normalize_country_code(vat_country or billing_country or "")


def buyer_country_from_snapshot(snapshot: BuyerBillingSnapshot) -> str:
    """:func:`buyer_country` read from a checkout billing snapshot."""
    return buyer_country(snapshot.get("vat_country_code"), snapshot.get("billing_country_code"))


def sale_decision(org: Organization, event: Event | None, buyer_country: str) -> Decision:
    """Whether Revel may invoice this sale for a scheme organizer; always allowed for any other organizer.

    See :func:`events.utils.small_business.scheme_sale_block`: a physical event held outside the
    scheme's country, or a virtual event bought from outside it, is not invoiced. An unknown event
    (deleted since the sale) is read as domestic.
    """
    if event is None or not is_small_business(org):
        return Decision.allow()
    home = scheme_country(org.small_business_scheme)
    code = scheme_sale_block(
        org.small_business_scheme,
        is_virtual=event.is_virtual,
        venue_country=event.effective_vat_country,
        buyer_country=buyer_country,
    )
    if not code:
        return Decision.allow()
    if code == SMALL_BUSINESS_FOREIGN_VENUE:
        message, country = FOREIGN_VENUE_BLOCKED_MESSAGE, normalize_country_code(event.effective_vat_country)
    else:
        message, country = VIRTUAL_CROSS_BORDER_BLOCKED_MESSAGE, normalize_country_code(buyer_country)
    return Decision.block(
        str(message).format(country=country_name(country), home=country_name(home)), code=code, country=home
    )


def invoice_sale_decision(invoice: AttendeeInvoice) -> Decision:
    """:func:`sale_decision` for a draft about to be issued.

    Never applied to credit notes: a credit note reverses the invoice it corrects as issued, whatever
    the organizer's scheme or the event's venue have become since.
    """
    if invoice.organization is None:
        return Decision.allow()
    country = buyer_country(invoice.buyer_vat_country, invoice.buyer_country_code)
    return sale_decision(invoice.organization, invoice.event, country)
