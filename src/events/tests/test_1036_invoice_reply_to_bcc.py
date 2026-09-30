"""Attendee invoice Reply-To/BCC only use verified or owner-only org addresses (#1036)."""

import typing as t
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization
from events.service.attendee_invoice_service import deliver_attendee_invoice, ensure_pdf_exists
from events.tests.test_attendee_invoice_delivery import _create_invoice

pytestmark = pytest.mark.django_db


def _deliver(org: Organization, event: Event, user: RevelUser, **org_fields: t.Any) -> dict[str, t.Any]:
    Organization.objects.filter(pk=org.pk).update(**org_fields)
    org.refresh_from_db()
    inv = _create_invoice(org, event, user)
    with (
        patch("events.service.attendee_invoice_service.render_pdf", return_value=b"fake-pdf"),
        patch("common.tasks.send_email") as mock_email,
    ):
        ensure_pdf_exists(inv)
        deliver_attendee_invoice(inv)
    return t.cast(dict[str, t.Any], mock_email.call_args.kwargs)


def test_unverified_contact_is_neither_reply_to_nor_bcc(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    kwargs = _deliver(
        organization, event, member_user, contact_email="c@org.test", contact_email_verified=False, billing_email=""
    )
    assert kwargs["reply_to"] is None
    assert kwargs["bcc"] is None


def test_verified_contact_is_reply_to_and_bcc_fallback(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    kwargs = _deliver(
        organization, event, member_user, contact_email="c@org.test", contact_email_verified=True, billing_email=""
    )
    assert kwargs["reply_to"] == ["c@org.test"]
    assert kwargs["bcc"] == ["c@org.test"]


def test_billing_email_is_bcc_but_never_reply_to(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    kwargs = _deliver(
        organization,
        event,
        member_user,
        contact_email="c@org.test",
        contact_email_verified=False,
        billing_email="billing@org.test",
    )
    assert kwargs["reply_to"] is None
    assert kwargs["bcc"] == ["billing@org.test"]


def test_billing_email_bcc_with_verified_contact_reply_to(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    kwargs = _deliver(
        organization,
        event,
        member_user,
        contact_email="c@org.test",
        contact_email_verified=True,
        billing_email="billing@org.test",
    )
    assert kwargs["reply_to"] == ["c@org.test"]
    assert kwargs["bcc"] == ["billing@org.test"]


def test_no_addresses_no_reply_to_no_bcc(organization: Organization, event: Event, member_user: RevelUser) -> None:
    kwargs = _deliver(
        organization, event, member_user, contact_email=None, contact_email_verified=False, billing_email=""
    )
    assert kwargs["reply_to"] is None
    assert kwargs["bcc"] is None
