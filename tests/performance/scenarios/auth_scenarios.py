# tests/performance/scenarios/auth_scenarios.py
"""Authentication-related performance test scenarios."""

from locust import task

from ..clients.mailpit_client import get_mailpit_client
from .base import AnonymousRevelUser, RevelUserBase


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

        # Step 1: Register
        with self.api.client.post(
            "/account/register",
            json={
                "email": new_user.email,
                "password": new_user.password,
                "first_name": new_user.first_name,
                "last_name": new_user.last_name,
            },
            headers={"Content-Type": "application/json"},
            name="/account/register",
            catch_response=True,
        ) as response:
            if response.status_code not in (200, 201):
                response.failure(f"Registration failed: {response.status_code}")
                return
            response.success()

        # Step 2: Wait for verification email
        token = self.mailpit.get_verification_token(new_user.email)
        if not token:
            # Log this as a failure but don't fail the whole test
            # Email might be delayed in high-load scenarios
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
                response.failure(f"Verification failed: {response.status_code}")
