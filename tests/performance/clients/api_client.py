# tests/performance/clients/api_client.py
"""Revel API client for Locust performance tests.

Provides JWT authentication handling and common API operations
that integrate with Locust's HttpUser for proper metrics collection.
"""

import logging
import typing as t
from dataclasses import dataclass

from config import config
from locust import HttpUser

logger = logging.getLogger(__name__)


@dataclass
class AuthTokens:
    """JWT authentication tokens."""

    access: str
    refresh: str


@dataclass
class UserContext:
    """Authenticated user context."""

    email: str
    tokens: AuthTokens | None = None
    user_id: str | None = None


class RevelAPIClient:
    """API client wrapper for Locust performance tests.

    Wraps Locust's HttpUser.client to provide:
    - JWT token management (login, refresh)
    - Common headers
    - Authenticated request helpers
    """

    def __init__(self, http_user: HttpUser) -> None:
        """Initialize the API client.

        Args:
            http_user: The Locust HttpUser instance for making requests.
        """
        self.client = http_user.client
        self.user_context: UserContext | None = None

    def _get_headers(self, authenticated: bool = True) -> dict[str, str]:
        """Get request headers.

        Args:
            authenticated: Whether to include Authorization header.

        Returns:
            Headers dict.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en",
        }
        if authenticated and self.user_context and self.user_context.tokens:
            headers["Authorization"] = f"Bearer {self.user_context.tokens.access}"
        return headers

    def login(self, email: str, password: str | None = None) -> bool:
        """Login and store JWT tokens.

        Args:
            email: User email address (also used as username).
            password: User password. Defaults to config.DEFAULT_PASSWORD.

        Returns:
            True if login successful, False otherwise.
        """
        password = password or config.DEFAULT_PASSWORD

        with self.client.post(
            "/auth/token/pair",
            json={"username": email, "password": password},
            headers=self._get_headers(authenticated=False),
            name="/auth/token/pair [login]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                self.user_context = UserContext(
                    email=email,
                    tokens=AuthTokens(
                        access=data["access"],
                        refresh=data["refresh"],
                    ),
                )
                response.success()
                return True
            else:
                logger.error(
                    "Login failed: status=%s, email=%s, response=%s",
                    response.status_code,
                    email,
                    response.text[:200],
                )
                response.failure(f"Login failed: {response.status_code}")
                return False

    def refresh_token(self) -> bool:
        """Refresh the access token using the refresh token.

        Returns:
            True if refresh successful, False otherwise.
        """
        if not self.user_context or not self.user_context.tokens:
            return False

        with self.client.post(
            "/auth/token/refresh",
            json={"refresh": self.user_context.tokens.refresh},
            headers=self._get_headers(authenticated=False),
            name="/auth/token/refresh",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                self.user_context.tokens.access = data["access"]
                response.success()
                return True
            else:
                response.failure(f"Token refresh failed: {response.status_code}")
                return False

    def get(
        self,
        path: str,
        name: str | None = None,
        params: dict[str, t.Any] | None = None,
        authenticated: bool = True,
    ) -> t.Any:
        """Make authenticated GET request.

        Args:
            path: API path (e.g., "/events").
            name: Name for Locust metrics grouping.
            params: Query parameters.
            authenticated: Whether to include auth header.

        Returns:
            Response object.
        """
        return self.client.get(
            path,
            headers=self._get_headers(authenticated=authenticated),
            params=params,
            name=name or path,
        )

    def post(
        self,
        path: str,
        json: dict[str, t.Any] | None = None,
        name: str | None = None,
        authenticated: bool = True,
    ) -> t.Any:
        """Make authenticated POST request.

        Args:
            path: API path.
            json: JSON body.
            name: Name for Locust metrics grouping.
            authenticated: Whether to include auth header.

        Returns:
            Response object.
        """
        return self.client.post(
            path,
            json=json,
            headers=self._get_headers(authenticated=authenticated),
            name=name or path,
        )

    def put(
        self,
        path: str,
        json: dict[str, t.Any] | None = None,
        name: str | None = None,
        authenticated: bool = True,
    ) -> t.Any:
        """Make authenticated PUT request.

        Args:
            path: API path.
            json: JSON body.
            name: Name for Locust metrics grouping.
            authenticated: Whether to include auth header.

        Returns:
            Response object.
        """
        return self.client.put(
            path,
            json=json,
            headers=self._get_headers(authenticated=authenticated),
            name=name or path,
        )

    def delete(
        self,
        path: str,
        name: str | None = None,
        authenticated: bool = True,
    ) -> t.Any:
        """Make authenticated DELETE request.

        Args:
            path: API path.
            name: Name for Locust metrics grouping.
            authenticated: Whether to include auth header.

        Returns:
            Response object.
        """
        return self.client.delete(
            path,
            headers=self._get_headers(authenticated=authenticated),
            name=name or path,
        )

    # Convenience methods for common API operations

    def get_me(self) -> dict[str, t.Any] | None:
        """Get current user info.

        Returns:
            User data dict or None if failed.
        """
        response = self.get("/account/me", name="/account/me")
        if response.status_code == 200:
            data = response.json()
            if self.user_context:
                self.user_context.user_id = data.get("id")
            return data
        return None

    def list_events(self, page: int = 1, page_size: int = 20) -> dict[str, t.Any] | None:
        """List public events.

        Args:
            page: Page number.
            page_size: Items per page.

        Returns:
            Paginated events response or None.
        """
        response = self.get(
            "/events/",
            params={"page": page, "page_size": page_size},
            name="/events/ [list]",
            authenticated=False,
        )
        if response.status_code == 200:
            return response.json()
        logger.error("list_events failed: status=%s, response=%s", response.status_code, response.text[:500])
        return None

    def get_event(self, event_id: str) -> dict[str, t.Any] | None:
        """Get event details by ID.

        Args:
            event_id: Event UUID.

        Returns:
            Event data or None.
        """
        response = self.get(
            f"/events/{event_id}",
            name="/events/{id} [detail]",
            authenticated=False,
        )
        if response.status_code == 200:
            return response.json()
        return None

    def get_event_by_slug(self, org_slug: str, event_slug: str) -> dict[str, t.Any] | None:
        """Get event details by slugs.

        Args:
            org_slug: Organization slug.
            event_slug: Event slug.

        Returns:
            Event data or None.
        """
        response = self.get(
            f"/events/{org_slug}/event/{event_slug}",
            name="/events/{org}/event/{slug} [detail]",
            authenticated=False,
        )
        if response.status_code == 200:
            return response.json()
        logger.error(
            "get_event_by_slug failed: status=%s, org=%s, event=%s, response=%s",
            response.status_code,
            org_slug,
            event_slug,
            response.text[:500],
        )
        return None

    def get_my_status(self, event_id: str) -> dict[str, t.Any] | None:
        """Get user's status for an event.

        Args:
            event_id: Event UUID.

        Returns:
            MyStatus response or None.
        """
        response = self.get(
            f"/events/{event_id}/my-status",
            name="/events/{id}/my-status [BOTTLENECK]",
        )
        if response.status_code == 200:
            return response.json()
        return None

    def rsvp(self, event_id: str, status: str = "yes") -> bool:
        """RSVP to an event.

        Args:
            event_id: Event UUID.
            status: RSVP status (yes, no, maybe).

        Returns:
            True if successful.
        """
        with self.client.post(
            f"/events/{event_id}/rsvp/{status}",
            headers=self._get_headers(),
            name=f"/events/{{id}}/rsvp/{status} [BOTTLENECK]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 201):
                response.success()
                return True
            else:
                logger.error(
                    "RSVP failed: status=%s, event=%s, rsvp_status=%s, user=%s, response=%s",
                    response.status_code,
                    event_id,
                    status,
                    self.user_context.email if self.user_context else "unknown",
                    response.text[:500],
                )
                response.failure(f"RSVP failed: {response.status_code}")
                return False

    def get_ticket_tiers(self, event_id: str) -> list[dict[str, t.Any]]:
        """Get ticket tiers for an event.

        Args:
            event_id: Event UUID.

        Returns:
            List of tier data.
        """
        response = self.get(
            f"/events/{event_id}/tickets/tiers",
            name="/events/{id}/tickets/tiers",
        )
        if response.status_code == 200:
            return response.json()  # Returns a plain list, not paginated
        return []

    def checkout_free(
        self,
        event_id: str,
        tier_id: str,
        guest_name: str,
    ) -> dict[str, t.Any] | None:
        """Checkout for a free ticket.

        Args:
            event_id: Event UUID.
            tier_id: Tier UUID.
            guest_name: Name for the ticket.

        Returns:
            Ticket data or None.
        """
        response = self.post(
            f"/events/{event_id}/tickets/{tier_id}/checkout",
            json={"tickets": [{"guest_name": guest_name}]},
            name="/events/{id}/tickets/{tier}/checkout [BOTTLENECK]",
        )
        if response.status_code in (200, 201):
            return response.json()
        logger.error(
            "checkout_free failed: status=%s, event=%s, tier=%s, response=%s",
            response.status_code,
            event_id,
            tier_id,
            response.text[:500],
        )
        return None

    def checkout_pwyc(
        self,
        event_id: str,
        tier_id: str,
        guest_name: str,
        price_per_ticket: str,
    ) -> dict[str, t.Any] | None:
        """Checkout for a PWYC ticket.

        Args:
            event_id: Event UUID.
            tier_id: Tier UUID.
            guest_name: Name for the ticket.
            price_per_ticket: Price to pay per ticket.

        Returns:
            Ticket data or None.
        """
        response = self.post(
            f"/events/{event_id}/tickets/{tier_id}/checkout/pwyc",
            json={
                "tickets": [{"guest_name": guest_name}],
                "price_per_ticket": price_per_ticket,
            },
            name="/events/{id}/tickets/{tier}/checkout/pwyc [BOTTLENECK]",
        )
        if response.status_code in (200, 201):
            return response.json()
        logger.error(
            "checkout_pwyc failed: status=%s, event=%s, tier=%s, price=%s, response=%s",
            response.status_code,
            event_id,
            tier_id,
            price_per_ticket,
            response.text[:500],
        )
        return None

    def get_questionnaire(self, event_id: str, questionnaire_id: str) -> dict[str, t.Any] | None:
        """Get questionnaire for submission.

        Args:
            event_id: Event UUID.
            questionnaire_id: Questionnaire UUID.

        Returns:
            Questionnaire data or None.
        """
        response = self.get(
            f"/events/{event_id}/questionnaire/{questionnaire_id}",
            name="/events/{id}/questionnaire/{qid}",
        )
        if response.status_code == 200:
            return response.json()
        return None

    def submit_questionnaire(
        self,
        event_id: str,
        questionnaire_id: str,
        answers: dict[str, t.Any],
    ) -> dict[str, t.Any] | None:
        """Submit questionnaire answers.

        Args:
            event_id: Event UUID.
            questionnaire_id: Questionnaire UUID.
            answers: Submission payload.

        Returns:
            Submission result or None.
        """
        response = self.post(
            f"/events/{event_id}/questionnaire/{questionnaire_id}/submit",
            json=answers,
            name="/events/{id}/questionnaire/{qid}/submit [BOTTLENECK]",
        )
        if response.status_code in (200, 201):
            return response.json()
        return None

    # Dashboard endpoints

    def get_dashboard_events(self, **filters: t.Any) -> dict[str, t.Any] | None:
        """Get dashboard events.

        Returns:
            Paginated events or None.
        """
        response = self.get("/dashboard/events", params=filters, name="/dashboard/events")
        if response.status_code == 200:
            return response.json()
        return None

    def get_dashboard_organizations(self) -> dict[str, t.Any] | None:
        """Get dashboard organizations.

        Returns:
            Paginated organizations or None.
        """
        response = self.get("/dashboard/organizations", name="/dashboard/organizations")
        if response.status_code == 200:
            return response.json()
        return None

    def get_dashboard_tickets(self) -> dict[str, t.Any] | None:
        """Get dashboard tickets.

        Returns:
            Paginated tickets or None.
        """
        response = self.get("/dashboard/tickets", name="/dashboard/tickets")
        if response.status_code == 200:
            return response.json()
        return None

    def get_dashboard_rsvps(self) -> dict[str, t.Any] | None:
        """Get dashboard RSVPs.

        Returns:
            Paginated RSVPs or None.
        """
        response = self.get("/dashboard/rsvps", name="/dashboard/rsvps")
        if response.status_code == 200:
            return response.json()
        return None
