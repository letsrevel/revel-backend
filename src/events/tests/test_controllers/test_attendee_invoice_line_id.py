"""Invoice lines expose the opaque ``line_id`` only, never the Payment or TicketTier ids behind them (#1242)."""

import typing as t
from uuid import uuid4

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice
from events.tests.test_controllers.test_attendee_invoice_endpoints import (
    _create_draft_invoice,
    _create_issued_invoice,
)

pytestmark = pytest.mark.django_db

LINE_ID, PAYMENT_ID, TIER_ID = str(uuid4()), str(uuid4()), str(uuid4())


def _with_ids(invoice: AttendeeInvoice) -> AttendeeInvoice:
    invoice.line_items = [{**invoice.line_items[0], "line_id": LINE_ID, "payment_id": PAYMENT_ID, "tier_id": TIER_ID}]
    invoice.save()
    return invoice


def _client(user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def _assert_only_line_id(line: dict[str, t.Any]) -> None:
    assert line["line_id"] == LINE_ID
    assert "payment_id" not in line
    assert "tier_id" not in line


def test_organizer_detail_exposes_line_id_only(
    organization_owner_client: Client, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = _with_ids(_create_draft_invoice(organization, event, member_user, suffix="lid1"))
    url = reverse("api:get_attendee_invoice", kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)})
    _assert_only_line_id(organization_owner_client.get(url).json()["line_items"][0])


def test_buyer_dashboard_exposes_line_id_only(organization: Organization, event: Event, member_user: RevelUser) -> None:
    _with_ids(_create_issued_invoice(organization, event, member_user, suffix="lid2"))
    response = _client(member_user).get(reverse("api:dashboard_invoices"))
    _assert_only_line_id(response.json()["results"][0]["line_items"][0])


def test_patch_echoing_line_id_keeps_the_payment_and_ignores_a_stated_one(
    organization_owner_client: Client, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = _with_ids(_create_draft_invoice(organization, event, member_user, suffix="lid3"))
    line = {k: v for k, v in invoice.line_items[0].items() if k not in ("payment_id", "tier_id")}
    line["payment_id"] = str(uuid4())  # not part of the input schema: dropped
    url = reverse("api:update_attendee_invoice", kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)})
    response = organization_owner_client.patch(
        url, data=orjson.dumps({"line_items": [line]}), content_type="application/json"
    )
    assert response.status_code == 200
    invoice.refresh_from_db()
    assert invoice.line_items[0]["line_id"] == LINE_ID
    assert invoice.line_items[0]["payment_id"] == PAYMENT_ID
    assert invoice.line_items[0]["tier_id"] == TIER_ID
