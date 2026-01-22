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

import logging
import os
from datetime import datetime


def setup_logging() -> None:
    """Configure file-based logging for performance tests.

    Creates a timestamped log file in the logs/ directory.
    Only ERROR level and above is written to file.
    """
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"perf_test_{timestamp}.log")

    # File handler - ERROR level only
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

    # Configure root logger (catches all)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)

    # Explicitly configure our package loggers to ensure propagation
    for logger_name in ["scenarios", "clients", "data"]:
        pkg_logger = logging.getLogger(logger_name)
        pkg_logger.setLevel(logging.DEBUG)
        pkg_logger.propagate = True  # Ensure errors propagate to root

    # Write a test entry to verify logging works
    test_logger = logging.getLogger("locustfile")
    test_logger.error("Performance test logging initialized - log file: %s", log_file)

    test_logger.info("Logging errors to: %s", log_file)


# Setup logging BEFORE importing scenarios (they use logging.getLogger(__name__))
setup_logging()

# Import all scenario classes so Locust can discover them
# These must be after setup_logging() so loggers are properly configured
from scenarios.auth_scenarios import ExistingUserLogin, NewUserRegistration  # noqa: E402
from scenarios.dashboard_scenarios import DashboardUser  # noqa: E402
from scenarios.discovery_scenarios import EventBrowser  # noqa: E402
from scenarios.questionnaire_scenarios import QuestionnaireUser  # noqa: E402
from scenarios.rsvp_scenarios import RSVPUser  # noqa: E402
from scenarios.ticket_scenarios import FreeTicketUser, PWYCTicketUser  # noqa: E402

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
