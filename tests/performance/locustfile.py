# tests/performance/locustfile.py
"""Main Locust entry point for Revel performance tests.

Usage:
    # Web UI mode
    cd tests/performance
    locust -f locustfile.py --host=http://localhost:8000/api

    # Headless mode
    locust -f locustfile.py --host=http://localhost:8000/api \
        --headless -u 100 -r 10 --run-time 5m

Environment Variables:
    LOCUST_BACKEND_URL: Backend API URL (default: http://localhost:8000/api)
    LOCUST_MAILPIT_URL: Mailpit URL (default: http://localhost:8025)
    LOCUST_DEFAULT_PASSWORD: Test user password (default: password123)
    LOCUST_NUM_PRESEEDED_USERS: Number of pre-seeded users (default: 100)

Pre-requisites:
    1. Run the data seeding command first:
       python src/manage.py bootstrap_perf_tests

    2. Ensure backend and Celery are running

    3. Ensure Mailpit is running (for registration tests)
"""

# Import all scenario classes so Locust can discover them
from scenarios.auth_scenarios import ExistingUserLogin, NewUserRegistration
from scenarios.dashboard_scenarios import DashboardUser
from scenarios.discovery_scenarios import EventBrowser
from scenarios.questionnaire_scenarios import QuestionnaireUser
from scenarios.rsvp_scenarios import RSVPUser
from scenarios.ticket_scenarios import FreeTicketUser, PWYCTicketUser

# Locust automatically discovers HttpUser subclasses
# The weight/tasks configuration is in each class

# For custom combined load profiles, you can create a master user:

# from locust import HttpUser, between
# from locust.user.task import TaskSetMeta
#
# class CombinedLoadTest(HttpUser):
#     """Combined load test with weighted scenarios."""
#     wait_time = between(1, 5)
#
#     # TaskSet approach for combining multiple user types
#     tasks = {
#         EventBrowser: 30,      # Discovery (high volume)
#         ExistingUserLogin: 20, # Auth (moderate)
#         DashboardUser: 15,     # Dashboard (complex queries)
#         RSVPUser: 15,          # RSVP (bottleneck)
#         FreeTicketUser: 10,    # Ticket checkout (bottleneck)
#         PWYCTicketUser: 5,     # PWYC checkout (bottleneck)
#         QuestionnaireUser: 5,  # Questionnaire (bottleneck)
#     }

# To run specific scenarios only, use the --class flag:
#   locust -f locustfile.py --host=http://localhost:8000/api --class RSVPUser
#
# To run multiple specific scenarios:
#   locust -f locustfile.py --host=http://localhost:8000/api \
#       --class RSVPUser --class FreeTicketUser

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
