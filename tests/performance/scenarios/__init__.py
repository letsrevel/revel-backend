# tests/performance/scenarios/__init__.py
"""Locust user scenarios for performance testing."""

from .auth_scenarios import ExistingUserLogin, NewUserRegistration
from .dashboard_scenarios import DashboardUser
from .discovery_scenarios import EventBrowser
from .questionnaire_scenarios import QuestionnaireUser
from .rsvp_scenarios import RSVPUser
from .ticket_scenarios import FreeTicketUser, PWYCTicketUser

__all__ = [
    "ExistingUserLogin",
    "NewUserRegistration",
    "EventBrowser",
    "DashboardUser",
    "RSVPUser",
    "FreeTicketUser",
    "PWYCTicketUser",
    "QuestionnaireUser",
]
