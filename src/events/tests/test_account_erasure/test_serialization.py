"""A checkout or a data export of an account never overlaps the deletion of that account (#1476).

The deletion task locks the account row first (NO KEY UPDATE). A checkout (``lock_account_for_checkout``)
and the export task take the same lock. A foreign-key insert alone does not serialize: Django's
constraints are deferred, so the insert locks the account only at commit.

``transaction=True`` is required for the threaded tests: each thread uses its own connection and must
see the other's real commits and lock waits, which the rollback-wrapped default ``django_db``
transaction (one connection, never committed) cannot provide.
"""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser, UserDataExport
from accounts.service.account_erasure import lock_account_for_checkout
from accounts.tasks import delete_user_account, generate_user_data_export
from events.models import Event, Ticket, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.tests.test_account_erasure.conftest import HOLDER_EMAIL, make_ticket
from events.tests.test_cancel_race_1431_1450 import _assert_waits_on_lock, _Pause, _Runner

Factory = t.Callable[[timedelta], Event]


def _account_lock(sql: str) -> bool:
    return "FOR NO KEY UPDATE" in sql and '"accounts_reveluser"' in sql


def _tier_lock(sql: str) -> bool:
    return "FOR UPDATE" in sql and '"events_tickettier"' in sql


def _export_row(sql: str) -> bool:
    return '"accounts_userdataexport"' in sql


@pytest.fixture
def free_tier(public_event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=public_event, name="Free", price=0, payment_method=TicketTier.PaymentMethod.FREE
    )


def _checkout(user_pk: t.Any, tier_pk: t.Any) -> t.Any:
    tier = TicketTier.objects.select_related("event").get(pk=tier_pk)
    user = RevelUser.objects.get(pk=user_pk)
    return BatchTicketService(tier.event, tier, user).create_batch(items=[TicketPurchaseItem(guest_name="Ada")])


@pytest.mark.django_db
class TestCheckoutRefusal:
    @pytest.mark.parametrize("field", ["erased_at", "deletion_requested_at"])
    def test_a_checkout_of_an_account_that_is_being_deleted_is_refused(
        self, holder: RevelUser, free_tier: TicketTier, field: str
    ) -> None:
        RevelUser.objects.filter(pk=holder.pk).update(**{field: timezone.now()})

        with pytest.raises(HttpError) as refused:
            _checkout(holder.pk, free_tier.pk)

        assert refused.value.status_code == 403
        assert not Ticket.objects.filter(user=holder).exists()

    def test_a_checkout_of_a_live_account_works(self, holder: RevelUser, free_tier: TicketTier) -> None:
        _checkout(holder.pk, free_tier.pk)

        assert Ticket.objects.filter(user=holder, status=Ticket.TicketStatus.ACTIVE).count() == 1

    def test_an_account_that_no_longer_exists_is_refused(self, holder: RevelUser) -> None:
        stale = RevelUser.objects.get(pk=holder.pk)
        holder.delete()

        with pytest.raises(HttpError) as refused:
            lock_account_for_checkout(stale)

        assert refused.value.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_a_deletion_waits_for_a_checkout_in_flight_and_then_keeps_its_ticket(
    holder: RevelUser, free_tier: TicketTier, ended_event_factory: Factory
) -> None:
    """The checkout holds its tier lock, before it inserts the ticket, when the deletion starts.

    Without the account lock the deletion does not wait: it reads the holdings without the new ticket, and
    the ticket then points at an erased account with no holder snapshot.
    """
    make_ticket(holder, ended_event_factory(-timedelta(days=3)))  # a kept ticket: the deletion erases in place
    runner = _Runner()
    pause = _Pause(_tier_lock)
    runner.start("checkout", lambda: _checkout(holder.pk, free_tier.pk), pause)
    assert pause.reached.wait(timeout=10), "the checkout never reached its tier lock"
    deletion = runner.start("deletion", lambda: delete_user_account(str(holder.pk)))
    _assert_waits_on_lock(deletion)
    pause.release.set()
    runner.join()

    assert runner.outcome["checkout"][0] == "ok", runner.outcome["checkout"]
    assert runner.outcome["deletion"][0] == "ok", runner.outcome["deletion"]
    holder.refresh_from_db()
    assert holder.erased_at is not None
    tickets = list(Ticket.objects.filter(user=holder))
    assert len(tickets) == 2
    assert {ticket.holder_email for ticket in tickets} == {HOLDER_EMAIL}


@pytest.mark.django_db(transaction=True)
def test_a_checkout_waits_for_a_deletion_in_flight_and_is_then_refused(
    holder: RevelUser, free_tier: TicketTier, ended_event_factory: Factory
) -> None:
    """The deletion holds the account row when the checkout starts: no ticket is written for the erased account."""
    kept = make_ticket(holder, ended_event_factory(-timedelta(days=3)))
    runner = _Runner()
    pause = _Pause(_account_lock)
    runner.start("deletion", lambda: delete_user_account(str(holder.pk)), pause)
    assert pause.reached.wait(timeout=10), "the deletion never reached its account lock"
    checkout = runner.start("checkout", lambda: _checkout(holder.pk, free_tier.pk))
    _assert_waits_on_lock(checkout)
    pause.release.set()
    runner.join()

    assert runner.outcome["deletion"][0] == "ok", runner.outcome["deletion"]
    status, error = runner.outcome["checkout"]
    assert status == "err" and isinstance(error, HttpError) and error.status_code == 403, runner.outcome["checkout"]
    assert list(Ticket.objects.filter(user=holder).values_list("pk", flat=True)) == [kept.pk]
    free_tier.refresh_from_db()
    assert free_tier.quantity_sold == 0


@pytest.mark.django_db(transaction=True)
def test_a_deletion_waits_for_a_data_export_in_flight_and_then_removes_it(
    holder: RevelUser, ended_event_factory: Factory, mailoutbox: list[t.Any]
) -> None:
    """The export holds the account row while it writes: the deletion cannot commit in the middle of it.

    Without the lock the deletion does not wait, and the export then writes the profile that it read before
    the erasure into a new file of the erased account.
    """
    make_ticket(holder, ended_event_factory(-timedelta(days=3)))
    runner = _Runner()
    pause = _Pause(_export_row)
    runner.start("export", lambda: generate_user_data_export(str(holder.pk)), pause)
    assert pause.reached.wait(timeout=10), "the export never reached its export row"
    deletion = runner.start("deletion", lambda: delete_user_account(str(holder.pk)))
    _assert_waits_on_lock(deletion)
    pause.release.set()
    runner.join()

    assert runner.outcome["export"][0] == "ok", runner.outcome["export"]
    assert runner.outcome["deletion"][0] == "ok", runner.outcome["deletion"]
    holder.refresh_from_db()
    assert holder.erased_at is not None
    assert not UserDataExport.objects.filter(user=holder).exists()
    # The export finished first, so its mail went out while the address still existed.
    assert [mail.subject for mail in mailoutbox].count("Your Revel Data Export is Ready") == 1
