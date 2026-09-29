"""Attendee invoice mail uses the deployment's apex domain, not a hardcoded one (#1029)."""

import typing as t
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization
from events.service.attendee_invoice_service import deliver_attendee_invoice, ensure_pdf_exists
from events.tests.test_attendee_invoice_delivery import _create_invoice

pytestmark = pytest.mark.django_db


@patch("events.service.attendee_invoice_service.render_pdf", return_value=b"fake-pdf")
@patch("common.tasks.send_email")
def test_invoice_from_uses_apex_domain(
    mock_email: t.Any,
    mock_pdf: t.Any,
    settings: t.Any,
    organization: Organization,
    event: Event,
    member_user: RevelUser,
) -> None:
    settings.DEFAULT_FROM_EMAIL = "Revel <noreply@selfhosted.example>"
    inv = _create_invoice(organization, event, member_user)
    ensure_pdf_exists(inv)

    deliver_attendee_invoice(inv)

    from_email = mock_email.call_args.kwargs["from_email"]
    assert from_email.endswith(f"<{organization.slug}@selfhosted.example>")
