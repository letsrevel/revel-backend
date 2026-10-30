"""Structured buyer billing address: checkout schema, snapshot and profile save."""

import typing as t

import pytest
from django.utils import timezone

from accounts.models import RevelUser, UserBillingProfile
from events.models.attendee_invoice import BuyerBillingSnapshot
from events.schema.checkout import BuyerBillingInfoSchema
from events.service.stripe_service import _build_billing_snapshot, _save_billing_to_profile
from events.utils.billing_address import snapshot_buyer_postal_address

ADDRESS: dict[str, str] = {
    "billing_address_line1": "Mariahilfer Str. 1",
    "billing_address_line2": "Top 4",
    "billing_postal_code": "1070",
    "billing_city": "Vienna",
    "billing_subdivision": "Wien",
    "billing_country_code": "at",
}
KEYS = list(ADDRESS)


def _info(**kwargs: str) -> BuyerBillingInfoSchema:
    return BuyerBillingInfoSchema.model_validate({"billing_name": "X", **kwargs})


def _snapshot(info: BuyerBillingInfoSchema) -> BuyerBillingSnapshot:
    return _build_billing_snapshot(info, vat_id_validated=False, reverse_charge=False, vat_id_status="none")


def test_snapshot_carries_structured_buyer_address() -> None:
    snapshot = _snapshot(_info(**ADDRESS))

    assert snapshot["billing_country_code"] == "AT"
    assert snapshot["billing_city"] == "Vienna"
    address = snapshot_buyer_postal_address(snapshot)
    assert address.is_complete
    assert address.line2 == "Top 4"


def test_old_client_payload_still_works() -> None:
    snapshot = _snapshot(_info())

    assert all(snapshot[k] == "" for k in KEYS)  # type: ignore[literal-required]


def test_buyer_country_falls_back_to_vat_prefix() -> None:
    snapshot = _snapshot(_info(vat_id="EL123456789"))

    assert snapshot_buyer_postal_address(snapshot).country_code == "GR"


def test_snapshot_without_new_keys_reads_as_empty() -> None:
    legacy = t.cast(
        BuyerBillingSnapshot,
        {
            "billing_name": "X",
            "vat_id": "",
            "vat_country_code": "",
            "vat_id_validated": False,
            "billing_address": "",
            "billing_email": "",
            "reverse_charge": False,
        },
    )

    assert snapshot_buyer_postal_address(legacy).is_empty


@pytest.mark.django_db
def test_save_to_profile_stores_structured_address(django_user_model: type[RevelUser]) -> None:
    user = django_user_model.objects.create_user(username="buyer@example.com", email="buyer@example.com", password="p")

    _save_billing_to_profile(user, _info(**ADDRESS))

    profile = UserBillingProfile.objects.get(user=user)
    assert profile.billing_address_line1 == "Mariahilfer Str. 1"
    assert profile.billing_postal_code == "1070"
    assert profile.billing_country_code == "AT"


@pytest.mark.django_db
def test_save_to_profile_keeps_structured_address_when_client_sends_none(
    django_user_model: type[RevelUser],
) -> None:
    """An older client that omits the structured fields must not wipe the saved profile address."""
    user = django_user_model.objects.create_user(username="old@example.com", email="old@example.com", password="p")
    _save_billing_to_profile(user, _info(**ADDRESS))

    _save_billing_to_profile(user, _info(billing_address="Free text 1"))

    profile = UserBillingProfile.objects.get(user=user)
    assert profile.billing_address == "Free text 1"
    assert profile.billing_address_line1 == "Mariahilfer Str. 1"
    assert profile.billing_city == "Vienna"
    assert profile.billing_country_code == "AT"


@pytest.mark.django_db
def test_save_to_profile_updates_only_the_structured_fields_sent(django_user_model: type[RevelUser]) -> None:
    user = django_user_model.objects.create_user(username="new@example.com", email="new@example.com", password="p")
    _save_billing_to_profile(user, _info(**ADDRESS))

    _save_billing_to_profile(user, _info(billing_city="Graz"))

    profile = UserBillingProfile.objects.get(user=user)
    assert profile.billing_city == "Graz"
    assert profile.billing_address_line1 == "Mariahilfer Str. 1"


def test_greek_billing_country_is_stored_as_iso_gr() -> None:
    """EN 16931 wants ISO ``GR`` for the address, so the VAT prefix ``el`` is accepted and normalized."""
    snapshot = _snapshot(_info(billing_country_code="el"))

    assert snapshot["billing_country_code"] == "GR"


@pytest.mark.django_db
def test_save_to_profile_resets_vies_state_when_vat_id_changes(django_user_model: type[RevelUser]) -> None:
    """A different VAT ID must not inherit the previous one's validated flag."""
    user = django_user_model.objects.create_user(username="vat@example.com", email="vat@example.com", password="p")
    UserBillingProfile.objects.create(
        user=user,
        vat_id="ATU12345678",
        vat_country_code="AT",
        vat_id_validated=True,
        vat_id_validated_at=timezone.now(),
        vies_request_identifier="req",
    )

    _save_billing_to_profile(user, _info(vat_id="DE123456789", vat_country_code="DE"))

    profile = UserBillingProfile.objects.get(user=user)
    assert profile.vat_id == "DE123456789"
    assert profile.vat_id_validated is False
    assert profile.vat_id_validated_at is None
    assert profile.vies_request_identifier == ""


@pytest.mark.django_db
def test_save_to_profile_keeps_vies_state_when_vat_id_unchanged(django_user_model: type[RevelUser]) -> None:
    user = django_user_model.objects.create_user(username="vat2@example.com", email="vat2@example.com", password="p")
    UserBillingProfile.objects.create(user=user, vat_id="ATU12345678", vat_country_code="AT", vat_id_validated=True)

    _save_billing_to_profile(user, _info(vat_id="ATU12345678", vat_country_code="AT"))

    assert UserBillingProfile.objects.get(user=user).vat_id_validated is True
