"""Tests for the GDPR export allowlist registry (#798).

Covers the two leak classes the registry exists to prevent (staff-actor
relations and organization business internals), the default-deny guard, and
the depth-2 completeness additions (referral payouts/statements, credit notes).
"""

import json
import typing as t
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone

from accounts.models import (
    ImpersonationLog,
    Referral,
    ReferralCode,
    ReferralPayout,
    ReferralPayoutStatement,
    RevelUser,
)
from accounts.service import gdpr
from conftest import RevelUserFactory
from events.models import (
    AttendeeInvoice,
    AttendeeInvoiceCreditNote,
    Event,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    Ticket,
    TicketTier,
)
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission


def _export(user: RevelUser) -> tuple[dict[str, t.Any], str]:
    """Generate an export and return (parsed JSON, raw JSON text)."""
    export = gdpr.generate_user_data_export(user)
    with zipfile.ZipFile(BytesIO(export.file.read())) as zip_file:
        raw = zip_file.read("revel_user_data.json").decode()
    return json.loads(raw), raw


def test_registry_covers_all_reverse_relations() -> None:
    """Every relation pointing at RevelUser must have an explicit export decision.

    If this test fails after adding a model with a user FK, add an ExportRule
    for the new accessor in accounts/service/gdpr.py — include it, or exclude
    it with a documented reason. Do NOT export third-party data (rows where
    the user is the acting staff member) or secrets.

    Hidden (``related_name='+'``) FKs count too, keyed
    ``<app_label>.<model>.<field>``: registering ``HistoricalRecords()`` on a
    user-linked model lands four of those at once.
    """
    relations = gdpr.get_user_reverse_relations()
    missing = set(relations) - set(gdpr.EXPORT_RULES)
    assert not missing, (
        f"Reverse relations without an export decision: {sorted(missing)}. "
        "Add an ExportRule for each in accounts/service/gdpr.py (see #798)."
    )
    stale = set(gdpr.EXPORT_RULES) - set(relations)
    assert not stale, f"EXPORT_RULES entries with no matching relation (typo or removed model): {sorted(stale)}"


def test_registry_shape_is_coherent() -> None:
    """Excluded rules document a reason; extra sections don't shadow relations."""
    for accessor, rule in gdpr.EXPORT_RULES.items():
        if not rule.include:
            assert rule.reason, f"excluded relation {accessor!r} needs a reason"
        else:
            assert not rule.reason, f"included relation {accessor!r} should not carry an exclusion reason"

    collisions = set(gdpr.EXTRA_SECTIONS) & (set(gdpr.get_user_reverse_relations()) | {"profile"})
    assert not collisions, f"EXTRA_SECTIONS keys collide with relation accessors: {sorted(collisions)}"


def test_hidden_relations_are_registered_and_never_exported() -> None:
    """Hidden FKs need a decision, and the only valid decision is "exclude".

    A ``related_name='+'`` relation has no attribute on the user, so there is
    nothing for the generic dump to read — an ``include=True`` rule for one
    would blow up at export time.
    """
    hidden = {key for key, rel in gdpr.get_user_reverse_relations().items() if rel.hidden}
    assert hidden <= set(gdpr.EXPORT_RULES), (
        f"hidden relations without a decision: {sorted(hidden - set(gdpr.EXPORT_RULES))}"
    )
    for key in hidden:
        assert not gdpr.EXPORT_RULES[key].include, f"hidden relation {key!r} cannot be exported"

    # The simple-history mirrors added with the subscriptions integration: the
    # subject columns *and* simple-history's own actor column.
    assert {
        "events.historicalmembershipsubscription.user",
        "events.historicalmembershipsubscription.history_user",
        "events.historicalcustomerprofile.user",
        "events.historicalcustomerprofile.history_user",
        "events.historicalmembershippayment.recorded_by",
        "events.historicalmembershippayment.history_user",
        "events.historicalmembershipsubscriptionplan.history_user",
    } <= hidden


def test_registry_guard_fails_when_a_hidden_decision_goes_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard actually bites: drop one hidden entry and it must fail."""
    key = "events.historicalmembershipsubscription.user"
    assert key in gdpr.get_user_reverse_relations()
    monkeypatch.setattr(gdpr, "EXPORT_RULES", {k: v for k, v in gdpr.EXPORT_RULES.items() if k != key})

    with pytest.raises(AssertionError, match=key):
        test_registry_covers_all_reverse_relations()


def test_actor_relations_are_excluded_by_rule() -> None:
    """The staff-actor and secret-bearing relations stay excluded."""
    for accessor in (
        "eventinvitationrequest_decided_by",
        "organizationmembershiprequest_decided_by",
        "checked_in_tickets",
        "cancelled_tickets",
        "recorded_membership_payments",
        "impersonations_performed",
        "questionnaireevaluation_set",
        "logentry_set",
        "file_exports",
        "eventtoken_tokens",
        "organizationtoken_tokens",
        "outstandingtoken_set",
        "global_bans",
        "blacklist_entries",
        "visible_attendees",
        "visible_to",
    ):
        assert not gdpr.EXPORT_RULES[accessor].include, f"{accessor} must not be exported"


@pytest.mark.django_db
def test_staff_export_contains_no_third_party_data(
    user: RevelUser,
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: RevelUserFactory,
) -> None:
    """A staff user's export must not contain data of users they acted upon."""
    attendee = revel_user_factory(username="attendee@example.com", email="attendee@example.com")
    start = timezone.now() + timedelta(days=7)
    event = Event.objects.create(
        organization=organization,
        name="Gala",
        slug="gala",
        start=start,
        end=start + timedelta(hours=4),
    )
    tier = TicketTier.objects.create(event=event, name="General")
    Ticket.objects.create(
        event=event,
        user=attendee,
        tier=tier,
        guest_name="Attendee",
        checked_in_by=user,
        checked_in_at=timezone.now(),
    )
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=attendee,
        decided_by=user,
        status=OrganizationMembershipRequest.Status.APPROVED,
    )
    ImpersonationLog.objects.create(admin_user=user, target_user=attendee, token_jti="jti-test-1")
    submission = QuestionnaireSubmission.objects.create(user=attendee, questionnaire=questionnaire)
    QuestionnaireEvaluation.objects.create(submission=submission, status="approved", evaluator=user)

    data, raw = _export(user)

    for accessor in (
        "checked_in_tickets",
        "organizationmembershiprequest_decided_by",
        "impersonations_performed",
        "questionnaireevaluation_set",
    ):
        assert accessor not in data
    assert str(attendee.id) not in raw
    assert attendee.email not in raw

    # The target's own export records that the impersonation happened, but not
    # the admin's identity/IP/user-agent nor the token id.
    attendee_data, attendee_raw = _export(attendee)
    (impersonation_data,) = attendee_data["impersonations_received"]
    assert impersonation_data["created_at"] is not None
    for excluded_field in ("admin_user", "token_jti", "ip_address", "user_agent"):
        assert excluded_field not in impersonation_data
    assert "jti-test-1" not in attendee_raw


@pytest.mark.django_db
def test_member_organizations_reduced_to_identity(
    user: RevelUser, organization: Organization, revel_user_factory: RevelUserFactory
) -> None:
    """A member sees the org's identity, never its billing/VAT/fee internals."""
    organization.vat_id = "ATU99999999"
    organization.billing_email = "finance@org.example"
    organization.save(update_fields=["vat_id", "billing_email"])
    member = revel_user_factory(username="member@example.com", email="member@example.com")
    OrganizationMember.objects.create(organization=organization, user=member)

    data, raw = _export(member)

    assert data["member_organizations"] == [
        {"id": str(organization.id), "name": organization.name, "slug": organization.slug}
    ]
    assert "ATU99999999" not in raw
    assert "finance@org.example" not in raw
    assert str(user.id) not in raw  # the org owner's identity


@pytest.mark.django_db
def test_owned_organizations_exclude_member_identifiers(
    user: RevelUser, organization: Organization, revel_user_factory: RevelUserFactory
) -> None:
    """The owner keeps their org business data but not member/staff pk lists."""
    member = revel_user_factory(username="member2@example.com", email="member2@example.com")
    OrganizationMember.objects.create(organization=organization, user=member)

    data, raw = _export(user)

    (org_data,) = data["owned_organizations"]
    assert "members" not in org_data
    assert "staff_members" not in org_data
    assert org_data["id"] == str(organization.id)
    assert str(member.id) not in raw


@pytest.mark.django_db
def test_referral_sections_exported_without_counterparty_ids(revel_user_factory: RevelUserFactory) -> None:
    """Referrer gets payouts/statements; neither side sees the other's user id."""
    referrer = revel_user_factory(username="referrer@example.com", email="referrer@example.com")
    referred = revel_user_factory(username="referred@example.com", email="referred@example.com")
    code = ReferralCode.objects.create(user=referrer, code="FRIEND10")
    referral = Referral.objects.create(referral_code=code, referred_user=referred)
    payout = ReferralPayout.objects.create(
        referral=referral,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        net_platform_fees=Decimal("100.00"),
        payout_amount=Decimal("10.00"),
        currency="EUR",
        status=ReferralPayout.ReferralPayoutStatus.PAID,
        stripe_transfer_id="tr_test_1",
    )
    statement = ReferralPayoutStatement.objects.create(
        payout=payout,
        document_type=ReferralPayoutStatement.DocumentType.PAYOUT_STATEMENT,
        document_number="RVL-RP-2026-000001",
        amount_gross=Decimal("10.00"),
        amount_net=Decimal("8.33"),
        amount_vat=Decimal("1.67"),
        vat_rate=Decimal("20.00"),
        referrer_name="Referrer Name",
        platform_business_name="Revel",
        platform_business_address="Somewhere 1, Vienna",
        platform_vat_id="ATU11111111",
    )
    statement.pdf_file.save("statement.pdf", ContentFile(b"%PDF-1.4"), save=True)

    referrer_data, referrer_raw = _export(referrer)

    (payout_data,) = referrer_data["referral_payouts"]
    assert payout_data["stripe_transfer_id"] == "tr_test_1"
    assert payout_data["status"] == ReferralPayout.ReferralPayoutStatus.PAID
    (statement_data,) = referrer_data["referral_payout_statements"]
    assert statement_data["document_number"] == "RVL-RP-2026-000001"
    assert "sig=" in statement_data["pdf_file"]  # protected file URL is signed
    (referral_data,) = referrer_data["referrals_made"]
    assert "referred_user" not in referral_data
    assert str(referred.id) not in referrer_raw
    assert referred.email not in referrer_raw

    referred_data, referred_raw = _export(referred)

    assert referred_data["referral"]["referral_code"] == "FRIEND10"
    assert str(referrer.id) not in referred_raw


@pytest.mark.django_db
def test_attendee_invoice_credit_notes_exported(
    user: RevelUser, organization: Organization, revel_user_factory: RevelUserFactory
) -> None:
    """Credit notes (depth-2 via the invoice) are part of the attendee's export."""
    attendee = revel_user_factory(username="buyer@example.com", email="buyer@example.com")
    start = timezone.now() + timedelta(days=7)
    event = Event.objects.create(
        organization=organization,
        name="Invoiced Event",
        slug="invoiced-event",
        start=start,
        end=start + timedelta(hours=2),
    )
    invoice = AttendeeInvoice.objects.create(
        organization=organization,
        event=event,
        user=attendee,
        stripe_session_id="cs_test_1",
        invoice_number="INV-2026-1",
        total_gross=Decimal("12.00"),
        total_net=Decimal("10.00"),
        total_vat=Decimal("2.00"),
        vat_rate=Decimal("20.00"),
        currency="EUR",
        seller_name="Test Organization",
        buyer_name="Buyer",
    )
    AttendeeInvoiceCreditNote.objects.create(
        invoice=invoice,
        credit_note_number="CN-2026-1",
        amount_gross=Decimal("12.00"),
        amount_net=Decimal("10.00"),
        amount_vat=Decimal("2.00"),
    )

    data, _ = _export(attendee)

    (note_data,) = data["attendee_invoice_credit_notes"]
    assert note_data["credit_note_number"] == "CN-2026-1"
    (invoice_data,) = data["attendee_invoices"]
    assert invoice_data["invoice_number"] == "INV-2026-1"


@pytest.mark.django_db
def test_request_rows_exclude_deciding_staff_id(
    user: RevelUser, organization: Organization, revel_user_factory: RevelUserFactory
) -> None:
    """The requester's own rows must not name the staff member who decided them.

    ``decided_by`` is the acting organizer's identity — third-party data on a
    row that is otherwise legitimately the requester's.
    """
    for accessor in (
        "whitelist_requests",
        "eventinvitationrequest_set",
        "organizationmembershiprequest_set",
    ):
        rule = gdpr.EXPORT_RULES[accessor]
        assert rule.include, f"{accessor} is the requester's own data and must be exported"
        assert "decided_by" in rule.exclude_fields, f"{accessor} must not leak the deciding staff member"

    requester = revel_user_factory(username="requester@example.com", email="requester@example.com")
    request = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=requester,
        decided_by=user,
        status=OrganizationMembershipRequest.Status.APPROVED,
    )

    data, raw = _export(requester)

    (request_data,) = data["organizationmembershiprequest_set"]
    assert request_data["id"] == str(request.id)
    assert request_data["status"] == OrganizationMembershipRequest.Status.APPROVED
    assert "decided_by" not in request_data
    assert str(user.id) not in raw


@pytest.mark.django_db
def test_membership_payments_exported_without_recorder_id(
    user: RevelUser, organization: Organization, revel_user_factory: RevelUserFactory
) -> None:
    """Subscribers get their payment history; the staff recorder stays out of it."""
    subscriber = revel_user_factory(username="subscriber@example.com", email="subscriber@example.com")
    tier = MembershipTier.objects.create(organization=organization, name="Pro")
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
    )
    period_start = timezone.now() - timedelta(days=30)
    period_end = timezone.now()
    subscription = MembershipSubscription.objects.create(
        user=subscriber,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_start=period_start,
        current_period_end=period_end,
    )
    MembershipPayment.objects.create(
        subscription=subscription,
        amount=Decimal("10.00"),
        currency="EUR",
        status=MembershipPayment.PaymentStatus.SUCCEEDED,
        period_start=period_start,
        period_end=period_end,
        recorded_by=user,
        notes="recorded offline",
    )

    data, raw = _export(subscriber)

    (payment_data,) = data["membership_payments"]
    assert payment_data["subscription"] == str(subscription.id)
    assert payment_data["amount"] == "10.00"
    assert payment_data["currency"] == "EUR"
    assert payment_data["notes"] == "recorded offline"
    assert "recorded_by" not in payment_data
    assert str(user.id) not in raw

    # The recorder's own export is about their organization, not the subscriber.
    recorder_data, recorder_raw = _export(user)

    assert "membership_payments" not in recorder_data or recorder_data["membership_payments"] == []
    assert str(subscriber.id) not in recorder_raw
    assert subscriber.email not in recorder_raw


@pytest.mark.django_db
def test_rows_carry_id_and_timestamps(
    user: RevelUser, organization: Organization, revel_user_factory: RevelUserFactory
) -> None:
    """Generic dumps re-add pk and timestamps that model_to_dict drops."""
    attendee = revel_user_factory(username="ts@example.com", email="ts@example.com")
    start = timezone.now() + timedelta(days=7)
    event = Event.objects.create(
        organization=organization,
        name="Timestamped",
        slug="timestamped",
        start=start,
        end=start + timedelta(hours=2),
    )
    tier = TicketTier.objects.create(event=event, name="General")
    ticket = Ticket.objects.create(event=event, user=attendee, tier=tier, guest_name="TS")

    data, _ = _export(attendee)

    (ticket_data,) = data["tickets"]
    assert ticket_data["id"] == str(ticket.id)
    assert ticket_data["created_at"] is not None
