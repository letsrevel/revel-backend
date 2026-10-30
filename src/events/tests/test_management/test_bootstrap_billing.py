"""Bootstrap billing seed: Austrian platform and structured org billing addresses."""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from common.models import SiteSettings
from events.management.commands.bootstrap_helpers.base import BootstrapState
from events.management.commands.bootstrap_helpers.billing import (
    configure_organization_billing,
    configure_platform_billing,
)
from events.models import Organization

pytestmark = pytest.mark.django_db


def test_platform_billing_seed_is_austrian() -> None:
    site = configure_platform_billing()

    site.refresh_from_db()
    assert site.platform_vat_country == "AT"
    assert site.platform_vat_rate == Decimal("20.00")
    assert site.platform_vat_id.startswith("ATU")
    assert SiteSettings.get_solo().platform_vat_country == "AT"


def test_bootstrap_orgs_have_structured_addresses(django_user_model: type[RevelUser]) -> None:
    state = BootstrapState()
    for key in ("alpha", "beta"):
        owner = django_user_model.objects.create_user(username=f"{key}@example.com", email=f"{key}@example.com")
        state.orgs[key] = Organization.objects.create(name=f"Org {key}", slug=f"org-{key}", owner=owner)

    configure_organization_billing(state)

    for key, country in (("alpha", "AT"), ("beta", "DE")):
        org = state.orgs[key]
        org.refresh_from_db()
        assert org.billing_postal_address().is_complete
        assert org.billing_country_code == country == org.vat_country_code
