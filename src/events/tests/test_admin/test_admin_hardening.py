"""Permission and integrity hardening of admin actions, inlines and saves."""

import typing as t

import pytest
from django.contrib.admin.sites import site
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory

from accounts.models import RevelUser
from events.admin.base import TicketInline
from events.admin.organization import OrganizationAdmin
from events.models import MembershipPayment, Organization

pytestmark = pytest.mark.django_db


def _view_only_staff() -> RevelUser:
    user = RevelUser.objects.create_user(
        username="viewonly", email="viewonly@example.com", password="pw", is_staff=True
    )
    user.user_permissions.add(*Permission.objects.filter(codename__startswith="view_"))
    return RevelUser.objects.get(pk=user.pk)


def test_ban_and_retry_actions_require_change_permission(rf: RequestFactory) -> None:
    """View-only staff are not offered the ban / retry-delivery actions."""
    from accounts.models import RevelUser as U
    from events.models.attendee_invoice import AttendeeInvoice

    request = rf.get("/")
    request.user = _view_only_staff()
    assert "ban_selected_users" not in site._registry[U].get_actions(request)
    assert "retry_delivery" not in site._registry[AttendeeInvoice].get_actions(request)


def test_ticket_inline_snapshots_are_readonly() -> None:
    """The financial snapshot fields are not editable through the inline."""
    inline = TicketInline(Organization, site)
    for field in ("refund_policy_snapshot", "price_paid", "discount_amount", "offline_refund_amount", "attribution"):
        assert field in inline.readonly_fields


def test_history_revert_post_requires_change_permission(rf: RequestFactory) -> None:
    """A view-only user can not POST a history revert."""
    admin_instance: t.Any = site._registry[MembershipPayment]
    request = rf.post("/")
    request.user = _view_only_staff()
    with pytest.raises(PermissionDenied):
        admin_instance.history_form_view(request, "1", "1")


def test_org_admin_resets_derived_state_on_identity_change(rf: RequestFactory, organization: Organization) -> None:
    """Changing vat_id / contact_email in the admin clears validation / verification."""
    organization.vat_id_validated = True
    organization.contact_email_verified = True
    organization.vat_id = "ATU12345678"
    organization.contact_email = "old@example.com"
    organization.save()
    admin_instance = t.cast(OrganizationAdmin, site._registry[Organization])

    class _Form:
        changed_data = ["vat_id", "contact_email"]

    organization.vat_id = "ATU87654321"
    organization.contact_email = "new@example.com"
    admin_instance.save_model(rf.post("/"), organization, _Form(), True)

    organization.refresh_from_db()
    assert organization.vat_id_validated is False
    assert organization.vat_id_validated_at is None
    assert organization.contact_email_verified is False
