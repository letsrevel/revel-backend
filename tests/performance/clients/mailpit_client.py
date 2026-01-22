# tests/performance/clients/mailpit_client.py
"""Mailpit API client for email verification during performance tests.

Uses the Mailpit API v1 to:
- Search for emails by recipient
- Extract verification tokens from email bodies
- Clear inbox between test runs
"""

import re
import time
from dataclasses import dataclass

import requests
from config import config


@dataclass
class EmailMessage:
    """Represents an email message from Mailpit."""

    id: str
    subject: str
    from_address: str
    to_addresses: list[str]
    created: str
    snippet: str


@dataclass
class EmailContent:
    """Full email content including body."""

    id: str
    subject: str
    text: str
    html: str


class MailpitClient:
    """Client for interacting with Mailpit API v1.

    API Documentation: https://mailpit.axllent.org/docs/api-v1/
    """

    # Regex pattern for extracting JWT tokens from email links
    # Matches: ?token=xxxxx.yyyyy.zzzzz (standard JWT format)
    TOKEN_PATTERN = re.compile(r"\?token=([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")

    def __init__(self, base_url: str | None = None) -> None:
        """Initialize Mailpit client.

        Args:
            base_url: Mailpit base URL. Defaults to config.MAILPIT_URL.
        """
        self.base_url = (base_url or config.MAILPIT_URL).rstrip("/")
        self.session = requests.Session()

    def _api_url(self, path: str) -> str:
        """Build full API URL."""
        return f"{self.base_url}/api/v1{path}"

    def list_messages(self, limit: int = 50) -> list[EmailMessage]:
        """List recent messages.

        Args:
            limit: Maximum number of messages to return.

        Returns:
            List of EmailMessage objects.
        """
        response = self.session.get(
            self._api_url("/messages"),
            params={"limit": limit},
        )
        response.raise_for_status()
        data = response.json()

        return [
            EmailMessage(
                id=msg["ID"],
                subject=msg.get("Subject", ""),
                from_address=msg.get("From", {}).get("Address", ""),
                to_addresses=[addr.get("Address", "") for addr in msg.get("To", [])],
                created=msg.get("Created", ""),
                snippet=msg.get("Snippet", ""),
            )
            for msg in data.get("messages", [])
        ]

    def search_messages(self, query: str, limit: int = 50) -> list[EmailMessage]:
        """Search messages by query.

        Args:
            query: Search query (e.g., "to:user@example.com").
            limit: Maximum number of messages to return.

        Returns:
            List of matching EmailMessage objects.
        """
        response = self.session.get(
            self._api_url("/search"),
            params={"query": query, "limit": limit},
        )
        response.raise_for_status()
        data = response.json()

        return [
            EmailMessage(
                id=msg["ID"],
                subject=msg.get("Subject", ""),
                from_address=msg.get("From", {}).get("Address", ""),
                to_addresses=[addr.get("Address", "") for addr in msg.get("To", [])],
                created=msg.get("Created", ""),
                snippet=msg.get("Snippet", ""),
            )
            for msg in data.get("messages", [])
        ]

    def get_message(self, message_id: str) -> EmailContent:
        """Get full message content.

        Args:
            message_id: The message ID.

        Returns:
            EmailContent with full text and HTML body.
        """
        response = self.session.get(self._api_url(f"/message/{message_id}"))
        response.raise_for_status()
        data = response.json()

        return EmailContent(
            id=data["ID"],
            subject=data.get("Subject", ""),
            text=data.get("Text", ""),
            html=data.get("HTML", ""),
        )

    def delete_all_messages(self) -> None:
        """Delete all messages from the inbox.

        Useful for cleanup between test runs.
        """
        response = self.session.delete(self._api_url("/messages"))
        response.raise_for_status()

    def delete_message(self, message_id: str) -> None:
        """Delete a specific message.

        Args:
            message_id: The message ID to delete.
        """
        response = self.session.delete(self._api_url("/messages"), json={"ids": [message_id]})
        response.raise_for_status()

    def extract_token_from_email(self, email_content: EmailContent) -> str | None:
        """Extract JWT token from email content.

        Searches both text and HTML content for verification/reset links.

        Args:
            email_content: The email content to search.

        Returns:
            The extracted JWT token, or None if not found.
        """
        # Try text body first
        match = self.TOKEN_PATTERN.search(email_content.text)
        if match:
            return match.group(1)

        # Fall back to HTML
        match = self.TOKEN_PATTERN.search(email_content.html)
        if match:
            return match.group(1)

        return None

    def wait_for_email(
        self,
        to_email: str,
        subject_contains: str | None = None,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> EmailMessage | None:
        """Wait for an email to arrive.

        Polls Mailpit until an email matching the criteria arrives or timeout.

        Args:
            to_email: Expected recipient email address.
            subject_contains: Optional substring that must be in subject.
            timeout: Maximum time to wait in seconds. Defaults to config value.
            poll_interval: Time between polls in seconds. Defaults to config value.

        Returns:
            The matching EmailMessage, or None if timeout reached.
        """
        timeout = timeout or config.EMAIL_POLL_TIMEOUT
        poll_interval = poll_interval or config.EMAIL_POLL_INTERVAL
        start_time = time.time()

        while time.time() - start_time < timeout:
            messages = self.search_messages(f"to:{to_email}")

            for msg in messages:
                # Check if email matches criteria
                if to_email.lower() in [addr.lower() for addr in msg.to_addresses]:
                    if subject_contains is None or subject_contains.lower() in msg.subject.lower():
                        return msg

            time.sleep(poll_interval)

        return None

    def get_verification_token(
        self,
        to_email: str,
        timeout: float | None = None,
    ) -> str | None:
        """Wait for and extract verification token from email.

        Convenience method that combines waiting for email and token extraction.

        Args:
            to_email: The email address to check.
            timeout: Maximum time to wait.

        Returns:
            The verification token, or None if not found.
        """
        email_msg = self.wait_for_email(to_email, subject_contains="verify", timeout=timeout)
        if not email_msg:
            return None

        content = self.get_message(email_msg.id)
        return self.extract_token_from_email(content)

    def get_password_reset_token(
        self,
        to_email: str,
        timeout: float | None = None,
    ) -> str | None:
        """Wait for and extract password reset token from email.

        Args:
            to_email: The email address to check.
            timeout: Maximum time to wait.

        Returns:
            The reset token, or None if not found.
        """
        email_msg = self.wait_for_email(to_email, subject_contains="password", timeout=timeout)
        if not email_msg:
            return None

        content = self.get_message(email_msg.id)
        return self.extract_token_from_email(content)


# Convenience function for simple usage
def get_mailpit_client() -> MailpitClient:
    """Get a configured MailpitClient instance."""
    return MailpitClient()
