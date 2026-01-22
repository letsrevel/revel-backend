# tests/performance/scenarios/auth_scenarios.py
"""Authentication-related performance test scenarios."""

import logging

from clients.mailpit_client import get_mailpit_client
from locust import task
from scenarios.base import AnonymousRevelUser, RevelUserBase

logger = logging.getLogger(__name__)

# Header name for verification token in system testing mode
X_TEST_VERIFICATION_TOKEN = "X-Test-Verification-Token"


class ExistingUserLogin(RevelUserBase):
    """Scenario: Existing user login flow.

    Tests the login flow for pre-seeded users:
    1. POST /auth/token/pair
    2. GET /account/me

    Weight: 20 (moderate traffic)
    """

    abstract = False

    @task
    def login_flow(self) -> None:
        """Execute the login flow."""
        # Get a random pre-seeded user
        user = self.data.get_random_preseeded_user()

        # Attempt login
        if self.api.login(user.email, user.password):
            # Validate authentication works
            self.api.get_me()
        else:
            logger.error("Login failed for pre-seeded user: email=%s", user.email)


class NewUserRegistration(AnonymousRevelUser):
    """Scenario: New user registration with email verification.

    Tests the full registration flow:
    1. POST /account/register
    2. Poll Mailpit for verification email
    3. Extract token
    4. POST /account/verify

    Weight: 5 (lower traffic - expensive operation)
    """

    abstract = False

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.mailpit = get_mailpit_client()

    @task
    def registration_flow(self) -> None:
        """Execute the full registration flow."""
        # Generate a unique new user
        new_user = self.data.generate_new_user()
        token: str | None = None

        # Step 1: Register
        with self.api.client.post(
            "/account/register",
            json={
                "email": new_user.email,
                "password1": new_user.password,
                "password2": new_user.password,
                "first_name": new_user.first_name,
                "last_name": new_user.last_name,
                "accept_toc_and_privacy": True,
            },
            headers={"Content-Type": "application/json"},
            name="/account/register",
            catch_response=True,
        ) as response:
            if response.status_code not in (200, 201):
                logger.error(
                    "Registration failed: status=%s, email=%s, response=%s",
                    response.status_code,
                    new_user.email,
                    response.text[:500],
                )
                response.failure(f"Registration failed: {response.status_code}")
                return
            response.success()

            # Check for token in response header (system testing mode)
            token = response.headers.get(X_TEST_VERIFICATION_TOKEN)

        # Step 2: Get verification token
        if token:
            logger.debug("Got verification token from header for %s", new_user.email)
        else:
            # Fall back to Mailpit polling (legacy mode)
            token = self.mailpit.get_verification_token(new_user.email)
            if not token:
                logger.error(
                    "Email verification timeout: email=%s (no verification email received)",
                    new_user.email,
                )
                # Report to Locust as failure for metrics visibility
                self.environment.events.request.fire(
                    request_type="MAIL",
                    name="email_verification_wait",
                    response_time=0,
                    response_length=0,
                    exception=TimeoutError(f"No verification email for {new_user.email}"),
                    context={},
                )
                return

        # Step 3: Verify email
        with self.api.client.post(
            "/account/verify",
            json={"token": token},
            headers={"Content-Type": "application/json"},
            name="/account/verify",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                logger.error(
                    "Email verification failed: status=%s, email=%s, response=%s",
                    response.status_code,
                    new_user.email,
                    response.text[:500],
                )
                response.failure(f"Verification failed: {response.status_code}")
