"""Permission and integrity hardening of admin actions, inlines and saves."""

import typing as t
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.admin.sites import site
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

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
    for field in (
        "refund_policy_snapshot",
        "price_paid",
        "discount_amount",
        "offline_refund_amount",
        "attribution",
        "checked_in_by",
        "discount_code",
        "held_pass",
        "file_content_hash",
    ):
        assert field in inline.readonly_fields


def test_history_revert_post_requires_change_permission(rf: RequestFactory) -> None:
    """A view-only user can not POST a history revert."""
    admin_instance: t.Any = site._registry[MembershipPayment]
    request = rf.post("/")
    request.user = _view_only_staff()
    with pytest.raises(PermissionDenied):
        admin_instance.history_form_view(request, "1", "1")


def _save_org(rf: RequestFactory, org: Organization, user: RevelUser, changed: list[str]) -> None:
    class _Form:
        changed_data = changed

    request = rf.post("/")
    request.user = user
    t.cast(OrganizationAdmin, site._registry[Organization]).save_model(request, org, _Form(), True)
    org.refresh_from_db()


def test_org_admin_resets_derived_state_on_vat_change(
    rf: RequestFactory, organization: Organization, superuser: RevelUser
) -> None:
    """Changing vat_id in the admin clears validation and re-derives the country."""
    organization.vat_id_validated = True
    organization.vies_request_identifier = "WAPIAAAA"
    organization.vat_id = "ATU12345678"
    organization.vat_country_code = "AT"
    organization.save()
    organization.vat_id = "DE123456789"
    _save_org(rf, organization, superuser, ["vat_id"])
    assert organization.vat_id_validated is False
    assert organization.vat_id_validated_at is None
    assert organization.vies_request_identifier == ""
    assert organization.vat_country_code == "DE"


def test_user_email_actions_require_change_permission(rf: RequestFactory) -> None:
    """View-only staff are not offered the verification / password-reset email actions."""
    request = rf.get("/")
    request.user = _view_only_staff()
    actions = site._registry[RevelUser].get_actions(request)
    assert "send_verification_email" not in actions
    assert "send_password_reset_email" not in actions


def test_every_history_admin_is_guarded() -> None:
    """Every SimpleHistoryAdmin registered must use the guarded base (revert needs change permission)."""
    from simple_history.admin import SimpleHistoryAdmin

    from common.admin import GuardedHistoryAdmin

    unguarded = [
        type(a).__name__
        for a in site._registry.values()
        if isinstance(a, SimpleHistoryAdmin) and not isinstance(a, GuardedHistoryAdmin)
    ]
    assert unguarded == []


def test_vat_change_respects_explicit_validated_flag(
    rf: RequestFactory, organization: Organization, superuser: RevelUser
) -> None:
    """A superuser who sets vat_id_validated in the same save keeps it; derived metadata is still cleared."""
    organization.vies_request_identifier = "WAPIAAAA"
    organization.vat_id = "DE123456789"
    organization.vat_id_validated = True
    _save_org(rf, organization, superuser, ["vat_id", "vat_id_validated"])
    assert organization.vat_id_validated is True
    assert organization.vat_id_validated_at is None
    assert organization.vies_request_identifier == ""
    assert organization.vat_country_code == "DE"


def test_clearing_vat_id_keeps_submitted_country(
    rf: RequestFactory, organization: Organization, superuser: RevelUser
) -> None:
    """Clearing the VAT ID does not blank the declared country code."""
    organization.vat_id = ""
    organization.vat_country_code = "AT"
    _save_org(rf, organization, superuser, ["vat_id"])
    assert organization.vat_country_code == "AT"
    assert organization.vat_id_validated is False


def test_contact_verification_not_sent_when_cleared_or_flag_set(
    rf: RequestFactory, organization: Organization, superuser: RevelUser
) -> None:
    """No verification email for a cleared address or an explicitly set verified flag."""
    with patch("events.admin.organization.contact_service.create_and_send_contact_email_verification") as send:
        organization.contact_email = ""
        _save_org(rf, organization, superuser, ["contact_email"])
        organization.contact_email = "x@example.com"
        organization.contact_email_verified = True
        _save_org(rf, organization, superuser, ["contact_email", "contact_email_verified"])
    send.assert_not_called()


def _change_post_data(client: Client, org: Organization) -> dict[str, t.Any]:
    """Build a valid POST body for the org change view from the rendered form's initial data."""
    url = reverse("admin:events_organization_change", args=[org.pk])
    ctx = client.get(url).context
    data: dict[str, t.Any] = {}
    for name, field in ctx["adminform"].form.fields.items():
        value = ctx["adminform"].form.initial.get(name, field.initial)
        if value is None or value is False:
            continue
        if isinstance(value, (list, tuple, set)) or hasattr(value, "all"):
            data[name] = [getattr(v, "pk", v) for v in (value.all() if hasattr(value, "all") else value)]
        else:
            data[name] = getattr(value, "pk", value)
    for inline in ctx["inline_admin_formsets"]:
        prefix = inline.formset.prefix
        data.update({f"{prefix}-TOTAL_FORMS": 0, f"{prefix}-INITIAL_FORMS": 0, f"{prefix}-MIN_NUM_FORMS": 0})
        data[f"{prefix}-MAX_NUM_FORMS"] = 1000
    return data


@override_settings(
    STORAGES={**settings.STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
)
@pytest.mark.parametrize("new_email", ["", "new@example.com"])
def test_admin_view_contact_email_change_resets_contact_method(
    client: Client, organization: Organization, superuser: RevelUser, new_email: str
) -> None:
    """Clearing or changing the contact email through the real change view saves (no 500/validation error)."""
    organization.contact_email = "old@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.EMAIL
    organization.save()
    client.force_login(superuser)
    data = _change_post_data(client, organization)
    data["contact_email"] = new_email
    data["contact_email_verified"] = "on"  # the unchanged initial value; must not defeat the reset

    with patch("events.admin.organization.contact_service.create_and_send_contact_email_verification") as send:
        response = client.post(reverse("admin:events_organization_change", args=[organization.pk]), data)

    assert response.status_code == 302, getattr(response, "context", None) and response.context["adminform"].errors
    organization.refresh_from_db()
    assert (organization.contact_email or "") == new_email
    assert organization.contact_method == Organization.ContactMethod.NONE
    assert organization.contact_email_verified is False
    assert send.called is bool(new_email)
