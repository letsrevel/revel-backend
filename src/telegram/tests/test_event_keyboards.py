"""Tests for the event eligibility keyboard's membership wait-state buttons."""

import typing as t
import uuid
from unittest.mock import MagicMock

import pytest

from events.service.event_manager import EventUserEligibility, NextStep
from telegram.keyboards import PENDING_STATUS_CALLBACK, get_event_eligible_keyboard

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("next_step", "expected_text"),
    [
        (NextStep.WAIT_FOR_MEMBERSHIP_APPROVAL, "Membership application pending"),
        (NextStep.WAIT_FOR_MEMBERSHIP_QUESTIONNAIRE_EVALUATION, "Membership questionnaire under review"),
    ],
)
def test_membership_wait_steps_render_non_actionable_button(next_step: NextStep, expected_text: str) -> None:
    """Each membership wait step adds one status button wired to PENDING_STATUS_CALLBACK."""
    event = MagicMock()
    event.id = uuid.uuid4()
    eligibility = EventUserEligibility(allowed=False, event_id=event.id, reason="x", next_step=next_step)

    markup = get_event_eligible_keyboard(event, eligibility, MagicMock())

    buttons: list[t.Any] = [b for row in markup.inline_keyboard for b in row]
    status_buttons = [b for b in buttons if b.callback_data == PENDING_STATUS_CALLBACK]
    assert len(status_buttons) == 1
    assert expected_text in status_buttons[0].text
    # The event-details web-app button is still present and nothing else is actionable.
    assert len(buttons) == 2
