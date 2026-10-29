"""``vat_category`` on the attendee invoice detail response is a nullable enum (#1234)."""

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization
from events.schema.invoice import InvoiceLineItemSchema
from events.tests.test_attendee_invoice._helpers import _create_draft

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def test_legacy_line_is_null_and_new_line_is_s(
    owner_client: Client, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = _create_draft(organization, event, member_user)
    url = reverse("api:get_attendee_invoice", kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)})

    assert owner_client.get(url).json()["line_items"][0]["vat_category"] is None

    invoice.line_items = [{**invoice.line_items[0], "vat_category": "S"}]
    invoice.vat_exemptions = {"E": {"text": "Art. 132", "code": ""}}
    invoice.save()
    data = owner_client.get(url).json()
    assert data["line_items"][0]["vat_category"] == "S"
    assert data["vat_exemptions"] == {"E": {"text": "Art. 132", "code": ""}}


def test_openapi_lists_the_vat_categories() -> None:
    schema = InvoiceLineItemSchema.model_json_schema()
    assert schema["$defs"]["VatCategory"]["enum"] == ["S", "AE", "E", "G"]
