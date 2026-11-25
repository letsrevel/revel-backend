"""Tests for the invitation API: staff send/resend/bulk, the email task, and the public claim flow (#1319).

``on_commit`` callbacks only run inside ``django_capture_on_commit_callbacks(execute=True)`` (pytest-django wraps
each test in a rolled-back transaction); the email task is mocked at ``.delay`` unless the test is about the email.
"""

import typing as t
from datetime import timedelta
from unittest.mock import ANY, patch

import pytest
from celery.exceptions import Retry
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Blacklist, MembershipRecord, Organization, OrganizationMember
from events.service import member_register_linking as linking
from events.tasks import send_member_register_invite

from .conftest import RecordFactory, make_client

pytestmark = pytest.mark.django_db

APPLY_ASYNC = "events.tasks.member_register.send_member_register_invite.apply_async"


def _invite_url(organization: Organization, record: MembershipRecord) -> str:
    return reverse("api:invite_member_register_record", kwargs={"slug": organization.slug, "record_id": record.pk})


def _bulk_url(organization: Organization) -> str:
    return reverse("api:invite_all_member_register", kwargs={"slug": organization.slug})


def _preview(client: Client, token: str) -> t.Any:
    return client.get(reverse("api:preview_member_register_invite", kwargs={"token": token}))


def _claim(client: Client, token: str) -> t.Any:
    return client.post(reverse("api:claim_member_register_invite", kwargs={"token": token}))


def _patch_email(client: Client, organization: Organization, record: MembershipRecord, email: str) -> None:
    url = reverse("api:update_member_register_record", kwargs={"slug": organization.slug, "record_id": record.pk})
    assert client.patch(url, {"email": email}, content_type="application/json").status_code == 200


class TestSendInvite:
    def test_merged_record_cannot_be_invited(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        survivor = make_record(email="survivor@example.com")
        absorbed = make_record(merged_into=survivor)

        with patch(APPLY_ASYNC) as delay:
            response = owner_client.post(_invite_url(organization, absorbed))

        assert response.status_code == 400
        delay.assert_not_called()
        absorbed.refresh_from_db()
        assert absorbed.invite_token is None

    def test_send_sets_token_and_dispatches_once(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        record = make_record()

        with patch(APPLY_ASYNC) as delay, django_capture_on_commit_callbacks(execute=True):
            response = owner_client.post(_invite_url(organization, record))

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["invite_status"] == "invited" and body["invited_at"] is not None
        assert "invite_token" not in body
        record.refresh_from_db()
        assert record.invite_token and record.invited_at is not None
        # One positional arg (an old worker's signature) with the marker in a message header.
        delay.assert_called_once_with(
            args=(str(record.pk),), headers={"invite_marker": linking.invite_marker(record.invite_token)}
        )

    def test_resend_rotates_token(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        record = make_record()
        with patch(APPLY_ASYNC), django_capture_on_commit_callbacks(execute=True):
            owner_client.post(_invite_url(organization, record))
        record.refresh_from_db()
        old = t.cast(str, record.invite_token)
        cache.clear()  # the resend interval has passed

        with patch(APPLY_ASYNC) as delay, django_capture_on_commit_callbacks(execute=True):
            assert owner_client.post(_invite_url(organization, record)).status_code == 200

        record.refresh_from_db()
        assert record.invite_token != old
        assert _preview(Client(), old).status_code == 404
        assert _preview(Client(), t.cast(str, record.invite_token)).status_code == 200
        delay.assert_called_once()

    def test_no_email_is_400(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        assert owner_client.post(_invite_url(organization, make_record(email=""))).status_code == 400

    def test_linked_is_400(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory, claimant: RevelUser
    ) -> None:
        assert owner_client.post(_invite_url(organization, make_record(user=claimant))).status_code == 400

    def test_revoked_is_400(self, owner_client: Client, organization: Organization, make_record: RecordFactory) -> None:
        start = timezone.now() - timedelta(days=3)
        record = make_record(valid_from=start, valid_until=start, ended_reason=MembershipRecord.EndedReason.REVOKED)
        assert owner_client.post(_invite_url(organization, record)).status_code == 400

    def test_temporary_entry_is_400(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        record = make_record(kind=MembershipRecord.Kind.TEMPORARY)

        with patch(APPLY_ASYNC) as delay:
            response = owner_client.post(_invite_url(organization, record))

        assert response.status_code == 400
        delay.assert_not_called()
        record.refresh_from_db()
        assert record.invite_token is None

    def test_closed_standard_entry_is_400(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        record = make_record(
            valid_from=timezone.now() - timedelta(days=3), valid_until=timezone.now() - timedelta(days=1)
        )

        with patch(APPLY_ASYNC) as delay:
            response = owner_client.post(_invite_url(organization, record))

        assert response.status_code == 400
        delay.assert_not_called()
        record.refresh_from_db()
        assert record.invite_token is None

    def test_open_standard_entry_is_invited(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        record = make_record()

        with patch(APPLY_ASYNC) as delay, django_capture_on_commit_callbacks(execute=True):
            response = owner_client.post(_invite_url(organization, record))

        assert response.status_code == 200
        record.refresh_from_db()
        assert record.invite_token is not None
        delay.assert_called_once_with(args=(str(record.pk),), headers=ANY)

    def test_resend_to_invited_open_entry_still_works(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        record = make_record(invited_at=timezone.now() - timedelta(hours=2), invite_token="old-token")

        with patch(APPLY_ASYNC) as delay, django_capture_on_commit_callbacks(execute=True):
            response = owner_client.post(_invite_url(organization, record))

        assert response.status_code == 200
        record.refresh_from_db()
        assert record.invite_token != "old-token"
        delay.assert_called_once_with(args=(str(record.pk),), headers=ANY)

    def test_resend_within_cooldown_is_400(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A second invite right after the first is refused: token unchanged, one dispatch in total."""
        record = make_record()
        with patch(APPLY_ASYNC) as delay, django_capture_on_commit_callbacks(execute=True):
            assert owner_client.post(_invite_url(organization, record)).status_code == 200
            record.refresh_from_db()
            first_token = record.invite_token

            response = owner_client.post(_invite_url(organization, record))

        assert response.status_code == 400
        assert "sent recently" in response.json()["detail"]
        record.refresh_from_db()
        assert record.invite_token == first_token
        delay.assert_called_once_with(args=(str(record.pk),), headers=ANY)

    def test_resend_after_email_change_to_a_new_address_is_allowed(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """The interval is per address: correcting the email lets the new address be invited at once."""
        record = make_record(email="a@example.com")
        with patch(APPLY_ASYNC), django_capture_on_commit_callbacks(execute=True):
            assert owner_client.post(_invite_url(organization, record)).status_code == 200
        _patch_email(owner_client, organization, record, "b@example.com")

        with patch(APPLY_ASYNC) as delay, django_capture_on_commit_callbacks(execute=True):
            response = owner_client.post(_invite_url(organization, record))

        assert response.status_code == 200, response.content
        delay.assert_called_once_with(args=(str(record.pk),), headers=ANY)

    def test_email_changed_back_to_a_recent_address_is_still_refused(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A -> B -> A does not reset the interval for A."""
        record = make_record(email="a@example.com")
        with patch(APPLY_ASYNC), django_capture_on_commit_callbacks(execute=True):
            assert owner_client.post(_invite_url(organization, record)).status_code == 200
        _patch_email(owner_client, organization, record, "b@example.com")
        _patch_email(owner_client, organization, record, "A@Example.com")

        with patch(APPLY_ASYNC) as delay:
            response = owner_client.post(_invite_url(organization, record))

        assert response.status_code == 400
        delay.assert_not_called()

    def test_two_records_with_the_same_address_share_the_interval(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        first = make_record(email="same@example.com")
        second = make_record(email="Same@Example.com")
        with patch(APPLY_ASYNC) as delay, django_capture_on_commit_callbacks(execute=True):
            assert owner_client.post(_invite_url(organization, first)).status_code == 200
            assert owner_client.post(_invite_url(organization, second)).status_code == 400
        delay.assert_called_once_with(args=(str(first.pk),), headers=ANY)

    def test_resend_after_the_interval_is_allowed(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        record = make_record()
        with patch(APPLY_ASYNC), django_capture_on_commit_callbacks(execute=True):
            assert owner_client.post(_invite_url(organization, record)).status_code == 200
        cache.clear()
        with patch(APPLY_ASYNC), django_capture_on_commit_callbacks(execute=True):
            assert owner_client.post(_invite_url(organization, record)).status_code == 200

    def test_permissions(
        self,
        organization: Organization,
        make_record: RecordFactory,
        staff_client: Client,
        outsider_client: Client,
    ) -> None:
        record = make_record()
        url = _invite_url(organization, record)
        with patch(APPLY_ASYNC):
            assert staff_client.post(url).status_code == 200
        assert outsider_client.post(url).status_code in (403, 404)
        assert Client().post(url).status_code == 401

    def test_staff_without_manage_members_is_forbidden(
        self, organization: Organization, make_record: RecordFactory, plain_staff_client: Client
    ) -> None:
        assert plain_staff_client.post(_invite_url(organization, make_record())).status_code == 403

    def test_list_shows_invite_status_without_n_plus_one(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        claimant: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        url = reverse("api:list_member_register", kwargs={"slug": organization.slug})
        make_record(email="a@example.com")

        with CaptureQueriesContext(connection) as one:
            assert len(owner_client.get(url).json()["results"]) == 1

        make_record(email="b@example.com", invited_at=timezone.now(), invite_token="t1")
        make_record(email="c@example.com", user=claimant)
        for i in range(7):
            other = django_user_model.objects.create_user(username=f"u{i}", email=f"u{i}@example.com", password="pw")
            make_record(email=f"d{i}@example.com", user=other if i % 2 else None, duplicate_claimant=claimant)

        with CaptureQueriesContext(connection) as many:
            results = owner_client.get(url).json()["results"]

        assert len(results) == 10
        assert len(many) == len(one)
        by_email = {r["email"]: r["invite_status"] for r in results}
        assert by_email["a@example.com"] == "not_invited"
        assert by_email["b@example.com"] == "invited"
        assert by_email["c@example.com"] == "linked"


class TestInviteEligibilityFields:
    def test_list_reports_can_invite_and_blocked_reason(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        claimant: RevelUser,
    ) -> None:
        now = timezone.now()
        survivor = make_record(email="survivor@example.com")
        make_record(email="open@example.com")
        make_record(email="linked@example.com", user=claimant)
        make_record(email="merged@example.com", merged_into=survivor)
        make_record(email="")
        make_record(email="temp@example.com", kind=MembershipRecord.Kind.TEMPORARY)
        make_record(email="closed@example.com", valid_from=now - timedelta(days=3), valid_until=now - timedelta(days=1))
        make_record(
            email="revoked@example.com",
            valid_from=now - timedelta(days=3),
            valid_until=now - timedelta(days=3),
            ended_reason=MembershipRecord.EndedReason.REVOKED,
        )
        make_record(email="future@example.com", valid_from=now + timedelta(days=5))

        url = reverse("api:list_member_register", kwargs={"slug": organization.slug})
        results = {r["email"]: r for r in owner_client.get(url, {"include_merged": "true"}).json()["results"]}

        assert results["open@example.com"]["can_invite"] is True
        assert results["open@example.com"]["invite_blocked_reason"] is None
        blocked_emails = [
            "linked@example.com",
            "merged@example.com",
            "",
            "temp@example.com",
            "closed@example.com",
            "revoked@example.com",
            "future@example.com",
        ]
        for email in blocked_emails:
            row = results[email]
            assert row["can_invite"] is False, email
            assert row["invite_blocked_reason"], email


class TestBulkInvite:
    def test_bulk_respects_and_sets_the_address_interval(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        recent = make_record(email="recent@example.com")
        fresh = make_record(email="fresh@example.com")
        dup = make_record(email="Fresh@example.com")
        with patch(APPLY_ASYNC), django_capture_on_commit_callbacks(execute=True):
            assert owner_client.post(_invite_url(organization, recent)).status_code == 200
        MembershipRecord.objects.filter(pk=recent.pk).update(invited_at=None, invite_token=None)

        with patch(APPLY_ASYNC), django_capture_on_commit_callbacks(execute=True):
            body = owner_client.post(_bulk_url(organization)).json()

        assert body["invited"] == 1
        sent = list(MembershipRecord.objects.filter(pk__in=[fresh.pk, dup.pk], invited_at__isnull=False))
        assert len(sent) == 1
        assert MembershipRecord.objects.get(pk=recent.pk).invited_at is None
        # the address it just sent to is now inside the interval for a single invite too
        retry_target = sent[0]
        with patch(APPLY_ASYNC):
            assert owner_client.post(_invite_url(organization, retry_target)).status_code == 400

    def test_invites_only_open_unlinked_never_invited_with_email(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        claimant: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        start = timezone.now() - timedelta(days=9)
        eligible = [make_record(email="a@example.com"), make_record(email="b@example.com")]
        make_record(email="")  # no email
        make_record(email="x@example.com", user=claimant)  # linked
        make_record(email="y@example.com", invited_at=timezone.now(), invite_token="old")  # already invited
        make_record(email="z@example.com", valid_from=start, valid_until=start + timedelta(days=1))  # closed
        make_record(email="w@example.com", kind=MembershipRecord.Kind.TEMPORARY)  # not STANDARD

        with patch(APPLY_ASYNC) as delay, django_capture_on_commit_callbacks(execute=True):
            response = owner_client.post(_bulk_url(organization))

        assert response.status_code == 202, response.content
        assert response.json() == {"invited": 2}
        for record in eligible:
            record.refresh_from_db()
            assert record.invite_token and record.invited_at is not None
        assert delay.call_count == 2
        assert MembershipRecord.objects.filter(invite_token__isnull=False).count() == 3  # 2 new + the old one

    def test_literal_route_is_not_swallowed_by_record_routes(
        self, owner_client: Client, organization: Organization
    ) -> None:
        response = owner_client.post(_bulk_url(organization))
        assert response.status_code == 202 and response.json() == {"invited": 0}

    def test_permissions(self, organization: Organization, plain_staff_client: Client, outsider_client: Client) -> None:
        assert plain_staff_client.post(_bulk_url(organization)).status_code == 403
        assert outsider_client.post(_bulk_url(organization)).status_code in (403, 404)
        assert Client().post(_bulk_url(organization)).status_code == 401


class TestInviteEmail:
    @pytest.fixture(autouse=True)
    def _site(self) -> None:
        site = SiteSettings.get_solo()
        site.live_emails = True
        site.frontend_base_url = "https://app.example.test"
        site.save()

    def test_renders_with_claim_link(self, organization: Organization, make_record: RecordFactory) -> None:
        record = make_record(invite_token="abc123", invited_at=timezone.now())

        send_member_register_invite(str(record.pk))

        (message,) = mail.outbox
        assert message.to == ["pat@example.com"]
        assert organization.name in message.subject
        assert "https://app.example.test/member-invite/abc123" in message.body
        assert "Pat Register" in message.body

    def test_tenant_org_sends_as_the_org(
        self, organization: Organization, make_record: RecordFactory, settings: t.Any
    ) -> None:
        settings.ORG_EMAIL_DOMAIN = "mail.example.test"
        Organization.objects.filter(pk=organization.pk).update(
            custom_domain="events.example.com", custom_domain_enabled=True
        )
        record = make_record(invite_token="abc123", invited_at=timezone.now())

        with patch("events.tasks.member_register.send_email") as send_email:
            send_member_register_invite(str(record.pk))

        kwargs = send_email.call_args.kwargs
        assert kwargs["from_email"] and "mail.example.test" in kwargs["from_email"]
        assert kwargs["headers"]["Feedback-ID"].endswith(":member_register_invite:revel")
        assert "https://events.example.com/member-invite/abc123" in kwargs["body"]

    def test_plain_text_body_is_not_html_escaped(self, make_record: RecordFactory) -> None:
        """Names with ``&`` or apostrophes read as typed in the text part (escaping stays on in the HTML part)."""
        record = make_record(full_name="Rock & Roll O'Brien", invite_token="abc123", invited_at=timezone.now())
        send_member_register_invite(str(record.pk))
        (message,) = mail.outbox
        assert "Rock & Roll O'Brien" in message.body
        assert "&amp;" not in message.body

    def test_sends_the_current_token(self, make_record: RecordFactory) -> None:
        """A resend between dispatch and execution makes the latest link win; no token is in the task args."""
        record = make_record(invite_token="new", invited_at=timezone.now())
        send_member_register_invite(str(record.pk))
        (message,) = mail.outbox
        assert "/member-invite/new" in message.body

    def test_superseded_job_is_skipped_and_current_job_sends(self, make_record: RecordFactory) -> None:
        """Invite A, retarget to B, invite B before the tasks run: only the job for B's invitation sends."""
        record = make_record(email="a@example.com", invite_token="tok-a", invited_at=timezone.now())
        stale_marker = linking.invite_marker("tok-a")
        record.email, record.invite_token = "b@example.com", "tok-b"
        record.save(update_fields=["email", "invite_token", "updated_at"])

        send_member_register_invite.apply(args=(str(record.pk),), headers={"invite_marker": stale_marker}, throw=True)
        assert mail.outbox == []

        send_member_register_invite.apply(
            args=(str(record.pk),), headers={"invite_marker": linking.invite_marker("tok-b")}, throw=True
        )
        (message,) = mail.outbox
        assert message.to == ["b@example.com"]

    def test_no_marker_header_still_sends(self, make_record: RecordFactory) -> None:
        record = make_record(invite_token="tok", invited_at=timezone.now())
        send_member_register_invite.apply(args=(str(record.pk),), throw=True)
        assert len(mail.outbox) == 1

    def test_a_worker_exposes_the_header_on_request_headers(self) -> None:
        """Celery merges ``apply_async(headers=...)`` into the message headers (amqp.send_task_message), and a worker
        builds the task's ``Context`` from them; custom keys surface in ``request.headers`` (not as an argument)."""
        from celery import current_app
        from celery.app.task import Context

        message = current_app.amqp.as_task_v2("tid", send_member_register_invite.name, args=("rec",))
        message.headers.update({"invite_marker": "m1"})  # what send_task_message does with headers=
        request = Context(message.headers)
        assert request.headers == {"invite_marker": "m1"}

    def test_marker_does_not_expose_the_token(self) -> None:
        assert "tok-secret-value" not in linking.invite_marker("tok-secret-value")

    def test_failed_send_is_retried_and_keeps_the_slot(
        self, organization: Organization, make_record: RecordFactory
    ) -> None:
        """A send error with retries left schedules a retry; the resend slot stays held meanwhile."""
        record = make_record(invite_token="abc", invited_at=timezone.now())
        assert linking._claim_invite_slot(organization, record.email)

        with (
            patch("events.tasks.member_register.send_email", side_effect=OSError("smtp down")) as send,
            patch.object(send_member_register_invite, "retry", side_effect=Retry()) as retry,
        ):
            send_member_register_invite.apply(args=(str(record.pk),), retries=0, throw=False)

        assert send.call_count == 1
        retry.assert_called_once_with(exc=ANY, countdown=60)
        assert not linking._claim_invite_slot(organization, record.email)  # slot still held

    def test_exhausted_retries_release_the_resend_slot(
        self, organization: Organization, make_record: RecordFactory
    ) -> None:
        """Regression: a send that kept failing left the one-hour resend slot held with no email sent."""
        record = make_record(invite_token="abc", invited_at=timezone.now())
        assert linking._claim_invite_slot(organization, record.email)

        with patch("events.tasks.member_register.send_email", side_effect=OSError("smtp down")):
            result = send_member_register_invite.apply(
                args=(str(record.pk),), retries=send_member_register_invite.max_retries, throw=False
            )

        assert result.failed()
        assert linking._claim_invite_slot(organization, record.email)  # slot was released: a resend is allowed

    def test_skips_when_no_token(self, make_record: RecordFactory) -> None:
        record = make_record()
        send_member_register_invite(str(record.pk))
        assert mail.outbox == []

    def test_skips_when_linked(self, make_record: RecordFactory, claimant: RevelUser) -> None:
        record = make_record(invite_token="abc", user=claimant)
        send_member_register_invite(str(record.pk))
        assert mail.outbox == []

    def test_skips_when_record_gone(self, make_record: RecordFactory) -> None:
        record = make_record(invite_token="abc")
        pk = str(record.pk)
        record.delete()
        send_member_register_invite(pk)
        assert mail.outbox == []


class TestPublicClaim:
    def test_preview(self, organization: Organization, make_record: RecordFactory) -> None:
        make_record(invite_token="tok")

        response = _preview(Client(), "tok")

        assert response.status_code == 200
        assert response.json() == {
            "organization_name": organization.name,
            "organization_slug": organization.slug,
            "full_name": "Pat Register",
        }

    def test_preview_unknown_and_linked_are_404(self, make_record: RecordFactory, claimant: RevelUser) -> None:
        make_record(invite_token="used", user=claimant)
        assert _preview(Client(), "nope").status_code == 404
        assert _preview(Client(), "used").status_code == 404

    def test_new_person_with_other_email_claims(
        self,
        organization: Organization,
        make_record: RecordFactory,
        claimant: RevelUser,
        link_tier: t.Any,
    ) -> None:
        record = make_record(invite_token="tok")

        response = _claim(make_client(claimant), "tok")

        assert response.status_code == 200, response.content
        assert response.json() == {"organization_slug": organization.slug, "record_id": str(record.pk)}
        record.refresh_from_db()
        assert record.user == claimant and record.invite_token is None
        assert OrganizationMember.objects.get(organization=organization, user=claimant).tier == link_tier

    def test_unverified_account_can_claim(
        self, organization: Organization, make_record: RecordFactory, django_user_model: type[RevelUser]
    ) -> None:
        """The link itself is the proof; no verified email is needed."""
        record = make_record(invite_token="tok")
        user = django_user_model.objects.create_user(username="u", email="u@example.com", password="pw")

        assert _claim(make_client(user), "tok").status_code == 200
        record.refresh_from_db()
        assert record.user == user

    def test_second_claim_is_404(
        self,
        make_record: RecordFactory,
        claimant: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        make_record(invite_token="tok")
        other = django_user_model.objects.create_user(username="o", email="o@example.com", password="pw")

        assert _claim(make_client(claimant), "tok").status_code == 200
        assert _claim(make_client(other), "tok").status_code == 404

    def test_duplicate_is_409_and_flags_the_record(
        self, organization: Organization, make_record: RecordFactory, claimant: RevelUser
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=claimant)
        record = make_record(invite_token="tok")

        response = _claim(make_client(claimant), "tok")

        assert response.status_code == 409
        assert response.json()["code"] == "already_member"
        assert response.json()["detail"]
        record.refresh_from_db()
        assert record.user is None and record.duplicate_claimant == claimant

    def test_banned_is_403(
        self,
        organization: Organization,
        make_record: RecordFactory,
        claimant: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        Blacklist.objects.create(organization=organization, user=claimant, created_by=organization_owner_user)
        record = make_record(invite_token="tok")

        response = _claim(make_client(claimant), "tok")

        assert response.status_code == 403
        assert response.json()["detail"]
        assert "code" not in response.json()
        record.refresh_from_db()
        assert record.user is None

    def test_not_started_is_409_with_code(self, make_record: RecordFactory, claimant: RevelUser) -> None:
        make_record(invite_token="tok", valid_from=timezone.now() + timedelta(days=30))

        response = _claim(make_client(claimant), "tok")

        assert response.status_code == 409
        assert response.json()["code"] == "not_started"
        assert response.json()["detail"]

    def test_unknown_token_is_404(self, claimant: RevelUser) -> None:
        assert _claim(make_client(claimant), "nope").status_code == 404

    def test_guest_is_404(self, make_record: RecordFactory, django_user_model: type[RevelUser]) -> None:
        make_record(invite_token="tok")
        guest = django_user_model.objects.create_user(username="g", email="g@example.com", password="pw", guest=True)
        assert _claim(make_client(guest), "tok").status_code == 404

    def test_anonymous_is_401(self, make_record: RecordFactory) -> None:
        make_record(invite_token="tok")
        assert _claim(Client(), "tok").status_code == 401
