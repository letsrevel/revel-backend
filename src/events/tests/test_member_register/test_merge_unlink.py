"""Merging account-less entries and unlinking links/merges (#1319)."""

import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import MembershipRecord, MembershipTier, Organization, OrganizationMember
from events.service.member_register_reconciliation import unlink_record

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db

DAY = timedelta(days=1)


def _merge_url(organization: Organization, survivor: MembershipRecord) -> str:
    return reverse("api:merge_member_register_record", kwargs={"slug": organization.slug, "record_id": survivor.pk})


def _unlink_url(organization: Organization, record: MembershipRecord) -> str:
    return reverse("api:unlink_member_register_record", kwargs={"slug": organization.slug, "record_id": record.pk})


def _merge(client: Client, organization: Organization, survivor: MembershipRecord, absorbed: MembershipRecord) -> t.Any:
    return client.post(
        _merge_url(organization, survivor),
        data={"absorbed_record_id": str(absorbed.pk)},
        content_type="application/json",
    )


@pytest.fixture
def account(django_user_model: type[RevelUser]) -> RevelUser:
    """A verified account whose email matches no register entry."""
    return django_user_model.objects.create_user(
        username="acct@example.com", email="acct@example.com", password="pw", email_verified=True
    )


@pytest.fixture
def survivor(
    organization: Organization, account: RevelUser, link_tier: MembershipTier, make_record: RecordFactory
) -> MembershipRecord:
    """A linked open STANDARD record (its access row exists, so the mirror agrees with it)."""
    record = make_record(
        user=account, full_name="Survivor", email="acct@example.com", valid_from=timezone.now() - 5 * DAY
    )
    OrganizationMember.objects.create(
        organization=organization, user=account, tier=link_tier, status=OrganizationMember.MembershipStatus.ACTIVE
    )
    return record


class TestMerge:
    def test_open_into_open_takes_earliest_start_and_keeps_survivor_tier(
        self,
        owner_client: Client,
        organization: Organization,
        survivor: MembershipRecord,
        make_record: RecordFactory,
    ) -> None:
        other_tier = MembershipTier.objects.create(organization=organization, name="Other")
        original_from = survivor.valid_from
        earliest = timezone.now() - 40 * DAY
        absorbed = make_record(
            full_name="Dup",
            email="dup@example.com",
            tier=other_tier,
            tier_name="Other",
            valid_from=earliest,
            duplicate_claimant=survivor.user,
        )
        absorbed.invite_token = "tok"
        absorbed.save(update_fields=["invite_token"])

        response = _merge(owner_client, organization, survivor, absorbed)

        assert response.status_code == 200, response.content
        assert response.json()["id"] == str(survivor.pk)
        survivor.refresh_from_db()
        absorbed = MembershipRecord.objects.get(pk=absorbed.pk)
        assert survivor.valid_from == earliest
        assert survivor.tier_id != other_tier.pk
        assert survivor.user_id is not None
        assert absorbed.merged_into == survivor
        assert absorbed.merge_prior_valid_from == original_from
        assert absorbed.invite_token is None and absorbed.duplicate_claimant is None and absorbed.user is None

    def test_survivor_keeps_its_earlier_start(
        self, owner_client: Client, organization: Organization, survivor: MembershipRecord, make_record: RecordFactory
    ) -> None:
        original_from = survivor.valid_from
        absorbed = make_record(valid_from=timezone.now() - DAY)

        assert _merge(owner_client, organization, survivor, absorbed).status_code == 200

        survivor.refresh_from_db()
        assert survivor.valid_from == original_from

    def test_absorbed_counts_nowhere(
        self, owner_client: Client, organization: Organization, survivor: MembershipRecord, make_record: RecordFactory
    ) -> None:
        absorbed = make_record(valid_from=timezone.now() - 40 * DAY)
        now = timezone.now()
        assert absorbed in MembershipRecord.objects.active_on(now)
        invitable = MembershipRecord.objects.unlinked().filter(pk=absorbed.pk)
        assert invitable.exists()

        assert _merge(owner_client, organization, survivor, absorbed).status_code == 200

        assert absorbed not in MembershipRecord.objects.active_on(now)
        assert not MembershipRecord.objects.active_during(now - 100 * DAY, now + DAY).filter(pk=absorbed.pk).exists()
        assert not invitable.exists()
        day = (now - 20 * DAY).date()
        listed = owner_client.get(
            reverse("api:list_member_register", kwargs={"slug": organization.slug}), {"active_on": day.isoformat()}
        )
        assert str(absorbed.pk) not in {r["id"] for r in listed.json()["results"]}

    def test_closed_absorbed_is_attached(
        self, owner_client: Client, organization: Organization, survivor: MembershipRecord, make_record: RecordFactory
    ) -> None:
        absorbed = make_record(valid_from=timezone.now() - 90 * DAY, valid_until=timezone.now() - 60 * DAY)

        response = _merge(owner_client, organization, survivor, absorbed)

        assert response.status_code == 200
        absorbed.refresh_from_db()
        survivor.refresh_from_db()
        assert absorbed.user == survivor.user and absorbed.merged_into is None
        assert survivor.valid_from < timezone.now()

    def test_temporary_absorbed_is_attached(
        self, owner_client: Client, organization: Organization, survivor: MembershipRecord, make_record: RecordFactory
    ) -> None:
        absorbed = make_record(kind=MembershipRecord.Kind.TEMPORARY)

        assert _merge(owner_client, organization, survivor, absorbed).status_code == 200

        absorbed.refresh_from_db()
        assert absorbed.user == survivor.user and absorbed.merged_into is None

    def test_refusals(
        self,
        owner_client: Client,
        organization: Organization,
        survivor: MembershipRecord,
        make_record: RecordFactory,
        django_user_model: type[RevelUser],
    ) -> None:
        # same record
        assert _merge(owner_client, organization, survivor, survivor).status_code == 400
        # absorbed already linked
        linked = make_record(user=django_user_model.objects.create_user(username="l", email="l@x.io", password="p"))
        assert _merge(owner_client, organization, survivor, linked).status_code == 400
        # survivor closed, absorbed open
        closed_survivor = make_record(valid_from=timezone.now() - 9 * DAY, valid_until=timezone.now() - 8 * DAY)
        open_absorbed = make_record()
        assert _merge(owner_client, organization, closed_survivor, open_absorbed).status_code == 400
        # survivor unlinked, absorbed closed
        unlinked_survivor = make_record()
        closed_absorbed = make_record(valid_from=timezone.now() - 9 * DAY, valid_until=timezone.now() - 8 * DAY)
        assert _merge(owner_client, organization, unlinked_survivor, closed_absorbed).status_code == 400
        # merged on either side
        merged = make_record(merged_into=survivor)
        assert _merge(owner_client, organization, survivor, merged).status_code == 400
        assert _merge(owner_client, organization, merged, open_absorbed).status_code == 400
        open_absorbed.refresh_from_db()
        assert open_absorbed.merged_into is None

    def test_cross_org_is_404(
        self,
        owner_client: Client,
        organization: Organization,
        survivor: MembershipRecord,
        django_user_model: type[RevelUser],
    ) -> None:
        other = Organization.objects.create(
            name="Other",
            slug="other",
            owner=django_user_model.objects.create_user(username="o", email="o@x.io", password="p"),
        )
        foreign = MembershipRecord.objects.create(
            organization=other, full_name="F", valid_from=timezone.now(), source=MembershipRecord.Source.MANUAL
        )
        assert _merge(owner_client, organization, survivor, foreign).status_code == 404
        assert _merge(owner_client, organization, foreign, survivor).status_code == 404


class TestUnlink:
    def test_restores_a_merge(
        self, owner_client: Client, organization: Organization, survivor: MembershipRecord, make_record: RecordFactory
    ) -> None:
        original_from = survivor.valid_from
        absorbed = make_record(valid_from=timezone.now() - 40 * DAY)
        _merge(owner_client, organization, survivor, absorbed)

        response = owner_client.post(_unlink_url(organization, absorbed))

        assert response.status_code == 200, response.content
        survivor.refresh_from_db()
        absorbed.refresh_from_db()
        assert survivor.valid_from == original_from
        assert absorbed.merged_into is None and absorbed.merge_prior_valid_from is None and absorbed.user is None
        assert response.json()["merged_into_id"] is None

    def test_two_merges_unlinked_lifo(
        self, owner_client: Client, organization: Organization, survivor: MembershipRecord, make_record: RecordFactory
    ) -> None:
        original_from = survivor.valid_from
        first = make_record(valid_from=timezone.now() - 20 * DAY)
        second = make_record(valid_from=timezone.now() - 40 * DAY)
        _merge(owner_client, organization, survivor, first)
        _merge(owner_client, organization, survivor, second)
        survivor.refresh_from_db()
        assert survivor.valid_from == second.valid_from

        owner_client.post(_unlink_url(organization, second))
        survivor.refresh_from_db()
        assert survivor.valid_from == first.valid_from

        owner_client.post(_unlink_url(organization, first))
        survivor.refresh_from_db()
        assert survivor.valid_from == original_from

    def test_unmerge_keeps_a_start_staff_moved_since(
        self, owner_client: Client, organization: Organization, survivor: MembershipRecord, make_record: RecordFactory
    ) -> None:
        absorbed = make_record(valid_from=timezone.now() - 40 * DAY)
        _merge(owner_client, organization, survivor, absorbed)
        edited = timezone.now() - 100 * DAY
        MembershipRecord.objects.filter(pk=survivor.pk).update(valid_from=edited)

        assert owner_client.post(_unlink_url(organization, absorbed)).status_code == 200

        survivor.refresh_from_db()
        assert survivor.valid_from == edited

    def test_unmerge_never_pushes_an_ended_survivor_past_its_end(
        self, owner_client: Client, organization: Organization, survivor: MembershipRecord, make_record: RecordFactory
    ) -> None:
        MembershipRecord.objects.filter(pk=survivor.pk).update(valid_from=timezone.now() + 5 * DAY)
        absorbed = make_record(valid_from=timezone.now() - 40 * DAY)
        assert _merge(owner_client, organization, survivor, absorbed).status_code == 200
        ended = timezone.now() - DAY
        MembershipRecord.objects.filter(pk=survivor.pk).update(
            valid_until=ended, ended_reason=MembershipRecord.EndedReason.CANCELLED
        )

        response = owner_client.post(_unlink_url(organization, absorbed))

        assert response.status_code == 200, response.content
        survivor.refresh_from_db()
        assert survivor.valid_from <= survivor.valid_until  # type: ignore[operator]

    def test_unlink_with_a_stale_instance_is_409(
        self, organization: Organization, make_record: RecordFactory, django_user_model: type[RevelUser]
    ) -> None:
        linker = django_user_model.objects.create_user(username="l@example.com", email="l@example.com", password="pw")
        stale = make_record()
        MembershipRecord.objects.filter(pk=stale.pk).update(user=linker)

        with pytest.raises(HttpError) as exc:
            unlink_record(stale)

        assert exc.value.status_code == 409
        stale.refresh_from_db()
        assert stale.user == linker

    def test_open_link_reversal_cancels_the_members_subscriptions(
        self, owner_client: Client, organization: Organization, survivor: MembershipRecord, account: RevelUser
    ) -> None:
        with patch(
            "events.service.subscription.lifecycle.cancel_subscriptions_for_membership_loss"
        ) as cancel_subscriptions:
            assert owner_client.post(_unlink_url(organization, survivor)).status_code == 200

        cancel_subscriptions.assert_called_once_with(account, organization)

    def test_reverses_an_open_link_and_cancels_access(
        self,
        owner_client: Client,
        organization: Organization,
        survivor: MembershipRecord,
        account: RevelUser,
    ) -> None:
        response = owner_client.post(_unlink_url(organization, survivor))

        assert response.status_code == 200, response.content
        survivor.refresh_from_db()
        assert survivor.user is None and survivor.valid_until is None
        member = OrganizationMember.objects.get(organization=organization, user=account)
        assert member.status == OrganizationMember.MembershipStatus.CANCELLED
        assert not MembershipRecord.objects.open().filter(organization=organization, user=account).exists()
        # the record is a plain open unlinked entry again (invitable), and the mirror opened nothing new
        assert MembershipRecord.objects.filter(organization=organization).count() == 1
        assert survivor in MembershipRecord.objects.unlinked()

    def test_open_link_without_active_access_row_leaves_status_alone(
        self,
        owner_client: Client,
        organization: Organization,
        survivor: MembershipRecord,
        account: RevelUser,
    ) -> None:
        OrganizationMember.objects.filter(organization=organization, user=account).update(
            status=OrganizationMember.MembershipStatus.BANNED
        )

        assert owner_client.post(_unlink_url(organization, survivor)).status_code == 200

        member = OrganizationMember.objects.get(organization=organization, user=account)
        assert member.status == OrganizationMember.MembershipStatus.BANNED

    def test_closed_link_only_removes_the_user(
        self,
        owner_client: Client,
        organization: Organization,
        survivor: MembershipRecord,
        account: RevelUser,
        make_record: RecordFactory,
    ) -> None:
        closed = make_record(user=account, valid_from=timezone.now() - 90 * DAY, valid_until=timezone.now() - 60 * DAY)

        assert owner_client.post(_unlink_url(organization, closed)).status_code == 200

        closed.refresh_from_db()
        assert closed.user is None
        member = OrganizationMember.objects.get(organization=organization, user=account)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert MembershipRecord.objects.open().filter(organization=organization, user=account).count() == 1

    def test_plain_unlinked_record_is_400(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        assert owner_client.post(_unlink_url(organization, make_record())).status_code == 400

    def test_merge_of_an_entry_that_absorbed_others_is_400(
        self, owner_client: Client, organization: Organization, survivor: MembershipRecord, make_record: RecordFactory
    ) -> None:
        middle = make_record(valid_from=timezone.now() - 10 * DAY)
        child = make_record(valid_from=timezone.now() - 20 * DAY)
        assert _merge(owner_client, organization, middle, child).status_code == 200

        response = _merge(owner_client, organization, survivor, middle)

        assert response.status_code == 400
        middle.refresh_from_db()
        assert middle.merged_into is None

    def test_cross_org_is_404(
        self, owner_client: Client, organization: Organization, django_user_model: type[RevelUser]
    ) -> None:
        other = Organization.objects.create(
            name="Other",
            slug="other",
            owner=django_user_model.objects.create_user(username="o", email="o@x.io", password="p"),
        )
        foreign = MembershipRecord.objects.create(
            organization=other, full_name="F", valid_from=timezone.now(), source=MembershipRecord.Source.MANUAL
        )
        assert owner_client.post(_unlink_url(organization, foreign)).status_code == 404


class TestPermissions:
    @pytest.mark.parametrize("endpoint", ["merge", "unlink"])
    @pytest.mark.parametrize(
        ("client_fixture", "expected"),
        [("owner_client", 200), ("staff_client", 200), ("plain_staff_client", 403), ("outsider_client", 404)],
    )
    def test_matrix(
        self,
        endpoint: str,
        client_fixture: str,
        expected: int,
        request: pytest.FixtureRequest,
        organization: Organization,
        make_record: RecordFactory,
        survivor: MembershipRecord,
    ) -> None:
        client: Client = request.getfixturevalue(client_fixture)
        if endpoint == "merge":
            response = _merge(client, organization, survivor, make_record())
        else:
            response = client.post(_unlink_url(organization, survivor))
        assert response.status_code == expected, response.content

    @pytest.mark.parametrize("endpoint", ["merge", "unlink"])
    def test_anonymous_is_401(
        self, endpoint: str, organization: Organization, survivor: MembershipRecord, make_record: RecordFactory
    ) -> None:
        if endpoint == "merge":
            response = _merge(Client(), organization, survivor, make_record())
        else:
            response = Client().post(_unlink_url(organization, survivor))
        assert response.status_code == 401


def test_merge_keeps_the_survivors_own_duplicate_claim(
    owner_client: Client, organization: Organization, make_record: RecordFactory, claimant: RevelUser
) -> None:
    """An unlinked survivor's duplicate claim concerns another claimant and survives the merge."""
    survivor = make_record(full_name="Survivor", email="s@example.com", duplicate_claimant=claimant)
    absorbed = make_record(full_name="Absorbed", email="a@example.com", valid_from=timezone.now() - timedelta(days=30))

    assert _merge(owner_client, organization, survivor, absorbed).status_code == 200

    survivor.refresh_from_db()
    assert survivor.duplicate_claimant_id == claimant.pk
