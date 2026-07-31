"""Tests that the seeder's sweeps stay scoped to the objects it created (#840).

The seeder used to run global ``Model.objects.filter(...)`` sweeps with no run
scoping. Against a database that already contained ``bootstrap_test_events``
fixtures, that silently corrupted them — most visibly by refunding (and thus
cancelling) tickets on ``test-sold-out-event`` and decrementing its tier's
``quantity_sold``, so the event was no longer sold out.
"""

import io
import typing as t

import pytest
from django.contrib.gis.geos import Point
from django.core.management import call_command

from accounts.models import RevelUser
from events.management.commands.seeder.config import SeederConfig
from events.management.commands.seeder.interactions import InteractionSeeder
from events.management.commands.seeder.questionnaires import QuestionnaireSeeder
from events.management.commands.seeder.state import SeederState
from events.management.commands.seeder.tickets import TicketSeeder
from events.models import Blacklist, Organization, Payment, Ticket, TicketTier, WhitelistRequest
from geo.models import City
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db

ALWAYS_REFUND = {"succeeded": 0.0, "pending": 0.0, "failed": 0.0, "refunded": 1.0}


def _state(organizations: list[Organization]) -> SeederState:
    """A seeder state whose run "created" the given organizations."""
    state = SeederState()
    state.organizations = organizations
    return state


def _seeder_org() -> Organization:
    """An organization standing in for one the seeder created this run."""
    owner = RevelUser.objects.create_user(username="seeded.owner", email="seeded.owner@example.com", password="x")
    return Organization.objects.create(name="Seeded Org", slug="seeded-org", owner=owner)


@pytest.fixture
def vienna(db: t.Any) -> City:
    """The city ``bootstrap_test_events`` looks up (already present via geo fixtures)."""
    city, _ = City.objects.get_or_create(
        name="Vienna",
        country="Austria",
        defaults={
            "ascii_name": "Vienna",
            "iso2": "AT",
            "iso3": "AUT",
            "city_id": 1,
            "location": Point(16.3738, 48.2082),
        },
    )
    return city


def test_bootstrap_sold_out_fixture_survives_a_seed_run(vienna: City) -> None:
    """``bootstrap`` → ``seed`` must leave ``test-sold-out-event`` sold out (#840).

    Both halves of the old damage are asserted: the ``Ticket`` rows *and* the
    tier's denormalized ``quantity_sold`` counter.
    """
    call_command("bootstrap_test_events")
    tier = TicketTier.objects.get(event__slug="test-sold-out-event", name="General Admission")
    assert tier.payment_method == TicketTier.PaymentMethod.ONLINE
    assert tier.quantity_sold == 5

    config = SeederConfig(seed=1234)
    config.payment_status_weights = ALWAYS_REFUND  # the outcome that used to corrupt the fixture
    TicketSeeder(config=config, state=_state([_seeder_org()]), stdout=io.StringIO())._create_payments()

    tier.refresh_from_db()
    assert tier.quantity_sold == 5
    assert Ticket.objects.filter(tier=tier, status=Ticket.TicketStatus.ACTIVE).count() == 5
    assert not Ticket.objects.filter(tier=tier).exclude(status=Ticket.TicketStatus.ACTIVE).exists()
    assert not Payment.objects.filter(ticket__tier=tier).exists()


def test_payments_are_created_for_the_run_s_own_tickets(vienna: City) -> None:
    """Scoping must not stop the seeder from paying for its own tickets."""
    call_command("bootstrap_test_events")
    foreign_tier = TicketTier.objects.get(event__slug="test-sold-out-event", name="General Admission")
    org = foreign_tier.event.organization

    TicketSeeder(config=SeederConfig(seed=1234), state=_state([org]), stdout=io.StringIO())._create_payments()

    assert Payment.objects.filter(ticket__tier=foreign_tier).count() == 5


def test_evaluations_are_scoped_to_the_run_s_questionnaires() -> None:
    """A ready submission on a foreign questionnaire must not be auto-evaluated."""
    user = RevelUser.objects.create_user(username="submitter", email="submitter@example.com", password="x")
    foreign = Questionnaire.objects.create(name="Foreign questionnaire")
    QuestionnaireSubmission.objects.create(
        questionnaire=foreign, user=user, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )

    seeder = QuestionnaireSeeder(config=SeederConfig(seed=1234), state=SeederState(), stdout=io.StringIO())
    seeder._create_evaluations()

    assert not QuestionnaireEvaluation.objects.exists()


def test_whitelist_requests_are_scoped_to_the_run_s_organizations() -> None:
    """A foreign organization's blacklist must not sprout seeded whitelist requests."""
    owner = RevelUser.objects.create_user(username="foreign.owner", email="foreign.owner@example.com", password="x")
    foreign_org = Organization.objects.create(name="Foreign Org", slug="foreign-org", owner=owner)
    Blacklist.objects.create(organization=foreign_org, first_name="Banned", reason="test", created_by=owner)

    state = _state([_seeder_org()])
    state.regular_users = [owner]
    InteractionSeeder(config=SeederConfig(seed=1234), state=state, stdout=io.StringIO())._create_whitelist_requests()

    assert not WhitelistRequest.objects.exists()
