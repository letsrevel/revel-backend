"""Buyer billing info at checkout: the invoice snapshot and the optional save to the user's profile."""

import typing as t

from django.db import transaction
from django.utils import timezone

from accounts.models import RevelUser
from common.postal_address import PostalAddress
from events.models.attendee_invoice import BuyerBillingSnapshot

if t.TYPE_CHECKING:
    from accounts.models import UserBillingProfile
    from events.schema.ticket import BuyerBillingInfoSchema


def _build_billing_snapshot(
    billing_info: "BuyerBillingInfoSchema",
    vat_id_validated: bool,
    reverse_charge: bool,
    vat_id_status: str,
) -> BuyerBillingSnapshot:
    """Build a buyer billing snapshot dict from checkout billing info.

    ``vat_id_status`` records the tri-state VIES outcome (valid / invalid / unavailable),
    which the compliance gates read to tell real business buyers from mistyped VAT IDs.
    """
    return BuyerBillingSnapshot(
        billing_name=billing_info.billing_name,
        vat_id=billing_info.vat_id,
        vat_country_code=billing_info.vat_country_code,
        vat_id_validated=vat_id_validated,
        vat_id_status=vat_id_status,
        billing_address=billing_info.billing_address,
        billing_email=billing_info.billing_email,
        reverse_charge=reverse_charge,
        billing_address_line1=billing_info.billing_address_line1,
        billing_address_line2=billing_info.billing_address_line2,
        billing_postal_code=billing_info.billing_postal_code,
        billing_city=billing_info.billing_city,
        billing_subdivision=billing_info.billing_subdivision,
        billing_country_code=billing_info.billing_country_code,
    )


def _save_billing_to_profile(
    user: RevelUser, billing_info: "BuyerBillingInfoSchema", *, vat_id_validated: bool = False
) -> None:
    """Save buyer billing info to the user's billing profile.

    ``vat_id_validated`` is the fresh VIES result for ``billing_info.vat_id`` obtained at checkout
    (False when the ID was not checked on this path); it is recorded when the profile's VAT ID changes.
    """
    from accounts.models import UserBillingProfile

    with transaction.atomic():
        UserBillingProfile.objects.get_or_create(user=user)
        profile = UserBillingProfile.objects.select_for_update().get(user=user)  # serialize VAT-state compares
        _apply_billing_to_profile(profile, billing_info, vat_id_validated=vat_id_validated)


def _apply_billing_to_profile(
    profile: "UserBillingProfile", billing_info: "BuyerBillingInfoSchema", *, vat_id_validated: bool
) -> None:
    """Write ``billing_info`` onto the (locked) ``profile``."""
    from common.service.vies_service import normalize_vat_id

    address = PostalAddress.from_fields(billing_info, "billing_").to_fields("billing_")
    structured = {f: v for f, v in address.items() if f in billing_info.model_fields_set}  # sent fields only
    fields_to_update = ["billing_name", "billing_address", "billing_email", *structured]
    profile.billing_name = billing_info.billing_name
    profile.billing_address = billing_info.billing_address
    profile.billing_email = billing_info.billing_email
    for field, value in structured.items():
        setattr(profile, field, value)
    if billing_info.vat_id:
        normalized_vat_id = normalize_vat_id(billing_info.vat_id)
        if normalized_vat_id != normalize_vat_id(profile.vat_id):
            # A different VAT ID: drop the old ID's state, recording the checkout's fresh VIES result if any.
            profile.vat_id_validated = vat_id_validated
            profile.vat_id_validated_at = timezone.now() if vat_id_validated else None
            profile.vies_request_identifier = ""
            fields_to_update.extend(["vat_id_validated", "vat_id_validated_at", "vies_request_identifier"])
        profile.vat_id = billing_info.vat_id
        profile.vat_country_code = normalized_vat_id[:2]  # the ID's own prefix wins over a submitted country
        fields_to_update.extend(["vat_id", "vat_country_code"])
    profile.save(update_fields=fields_to_update)
