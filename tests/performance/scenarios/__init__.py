# tests/performance/scenarios/__init__.py
"""Locust user scenarios for performance testing."""

from scenarios.auth_scenarios import ExistingUserLogin, NewUserRegistration
from scenarios.dashboard_scenarios import DashboardUser
from scenarios.discovery_scenarios import EventBrowser
from scenarios.questionnaire_scenarios import QuestionnaireUser
from scenarios.rsvp_scenarios import RSVPUser
from scenarios.ticket_scenarios import FreeTicketUser, PWYCTicketUser

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
