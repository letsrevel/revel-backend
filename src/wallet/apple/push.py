"""Apple Push Notification service client for Wallet pass updates.

This module handles sending push notifications to Apple devices to trigger
wallet pass updates. When a pass needs updating (e.g., event details changed),
we send an empty push notification to the device, which then fetches the
updated pass from our web service.

Apple requires:
- HTTP/2 connection to api.push.apple.com (production) or api.sandbox.push.apple.com
- Authentication via Pass Type ID certificate (same cert used to sign passes)
- Empty JSON payload for wallet pass updates
- Topic header set to the Pass Type ID
"""

import ssl
from pathlib import Path

import httpx
import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)


# APNs endpoints
APNS_PRODUCTION_HOST = "api.push.apple.com"
APNS_SANDBOX_HOST = "api.sandbox.push.apple.com"
APNS_PORT = 443


class ApplePushError(Exception):
    """Raised when push notification fails.

    Attributes:
        status_code: HTTP status code from APNs, if available.
        reason: Error reason from APNs, if available.
    """

    def __init__(self, message: str, status_code: int | None = None, reason: str | None = None) -> None:
        """Initialize the error.

        Args:
            message: Error message describing what went wrong.
            status_code: HTTP status code from APNs, if available.
            reason: Error reason from APNs, if available.
        """
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class ApplePushNotificationClient:
    """Client for sending Apple Push Notifications for wallet pass updates.

    This client uses HTTP/2 to communicate with APNs. For wallet passes,
    we always use the production endpoint (not sandbox), and send an empty
    payload to trigger the device to fetch the updated pass.
    """

    def __init__(
        self,
        cert_path: str | None = None,
        key_path: str | None = None,
        key_password: str | None = None,
        pass_type_id: str | None = None,
        use_sandbox: bool = False,
    ) -> None:
        """Initialize the push notification client.

        Args:
            cert_path: Path to Pass Type ID certificate (PEM format).
            key_path: Path to private key (PEM format).
            key_password: Password for private key if encrypted.
            pass_type_id: The Pass Type ID (e.g., pass.com.example.ticket).
            use_sandbox: Whether to use sandbox APNs (usually False for wallet).
        """
        self.cert_path = cert_path or settings.APPLE_WALLET_CERT_PATH
        self.key_path = key_path or settings.APPLE_WALLET_KEY_PATH
        self.key_password = key_password or settings.APPLE_WALLET_KEY_PASSWORD
        self.pass_type_id = pass_type_id or settings.APPLE_WALLET_PASS_TYPE_ID
        self.use_sandbox = use_sandbox

        self._host = APNS_SANDBOX_HOST if use_sandbox else APNS_PRODUCTION_HOST
        self._client: httpx.Client | None = None

    def _get_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context with client certificate authentication.

        Returns:
            Configured SSL context.

        Raises:
            ApplePushError: If certificates cannot be loaded.
        """
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2

            # Load certificate and key
            cert_path = Path(self.cert_path)
            key_path = Path(self.key_path)

            if not cert_path.exists():
                raise ApplePushError(f"Certificate not found: {self.cert_path}")
            if not key_path.exists():
                raise ApplePushError(f"Key not found: {self.key_path}")

            context.load_cert_chain(
                certfile=str(cert_path),
                keyfile=str(key_path),
                password=self.key_password if self.key_password else None,
            )

            # Load default CA certificates for verifying Apple's server
            context.load_default_certs()

            return context

        except ssl.SSLError as e:
            raise ApplePushError(f"SSL configuration failed: {e}")

    def _get_client(self) -> httpx.Client:
        """Get or create the HTTP/2 client.

        Returns:
            Configured httpx client with HTTP/2 support.
        """
        if self._client is None:
            ssl_context = self._get_ssl_context()
            self._client = httpx.Client(
                http2=True,
                verify=ssl_context,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    def send_update_notification(self, push_token: str) -> bool:
        """Send a push notification to trigger pass update.

        For wallet passes, we send an empty JSON payload. The device will
        then call our web service to get the list of updated passes and
        fetch the new pass data.

        Args:
            push_token: The device push token from registration.

        Returns:
            True if notification was sent successfully.

        Raises:
            ApplePushError: If the notification fails to send.
        """
        url = f"https://{self._host}:{APNS_PORT}/3/device/{push_token}"

        headers = {
            "apns-topic": self.pass_type_id,
            "apns-push-type": "background",
            "apns-priority": "5",  # Low priority for background updates
        }

        # Empty payload for wallet pass updates
        payload = "{}"

        try:
            client = self._get_client()
            response = client.post(
                url,
                content=payload,
                headers=headers,
            )

            if response.status_code == 200:
                logger.info(
                    "push_notification_sent",
                    push_token=push_token[:20] + "...",
                    status=response.status_code,
                )
                return True

            # Handle error responses
            error_body = response.text
            reason = None
            try:
                error_json = response.json()
                reason = error_json.get("reason")
            except Exception:
                pass

            logger.warning(
                "push_notification_failed",
                push_token=push_token[:20] + "...",
                status=response.status_code,
                reason=reason,
                body=error_body[:200],
            )

            raise ApplePushError(
                f"APNs returned status {response.status_code}",
                status_code=response.status_code,
                reason=reason,
            )

        except httpx.RequestError as e:
            logger.error(
                "push_notification_request_error",
                push_token=push_token[:20] + "...",
                error=str(e),
            )
            raise ApplePushError(f"Request failed: {e}")

    def send_batch_notifications(self, push_tokens: list[str]) -> dict[str, bool]:
        """Send notifications to multiple devices.

        Args:
            push_tokens: List of device push tokens.

        Returns:
            Dictionary mapping push_token to success status.
        """
        results: dict[str, bool] = {}

        for token in push_tokens:
            try:
                success = self.send_update_notification(token)
                results[token] = success
            except ApplePushError as e:
                logger.warning(
                    "batch_notification_failed",
                    push_token=token[:20] + "...",
                    error=str(e),
                )
                results[token] = False

        success_count = sum(1 for v in results.values() if v)
        logger.info(
            "batch_notifications_complete",
            total=len(push_tokens),
            successful=success_count,
            failed=len(push_tokens) - success_count,
        )

        return results

    def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "ApplePushNotificationClient":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: type | None, exc_val: Exception | None, exc_tb: object) -> None:
        """Context manager exit."""
        self.close()

    def is_configured(self) -> bool:
        """Check if push notifications are properly configured.

        Returns:
            True if all required settings are present.
        """
        return bool(self.cert_path and self.key_path and self.pass_type_id)


class AsyncApplePushNotificationClient:
    """Async version of the Apple Push Notification client.

    Use this in async contexts (e.g., with asyncio) for better performance
    when sending many notifications.
    """

    def __init__(
        self,
        cert_path: str | None = None,
        key_path: str | None = None,
        key_password: str | None = None,
        pass_type_id: str | None = None,
        use_sandbox: bool = False,
    ) -> None:
        """Initialize the async push notification client."""
        self.cert_path = cert_path or settings.APPLE_WALLET_CERT_PATH
        self.key_path = key_path or settings.APPLE_WALLET_KEY_PATH
        self.key_password = key_password or settings.APPLE_WALLET_KEY_PASSWORD
        self.pass_type_id = pass_type_id or settings.APPLE_WALLET_PASS_TYPE_ID
        self.use_sandbox = use_sandbox

        self._host = APNS_SANDBOX_HOST if use_sandbox else APNS_PRODUCTION_HOST
        self._client: httpx.AsyncClient | None = None

    def _get_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context with client certificate authentication."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        cert_path = Path(self.cert_path)
        key_path = Path(self.key_path)

        if not cert_path.exists():
            raise ApplePushError(f"Certificate not found: {self.cert_path}")
        if not key_path.exists():
            raise ApplePushError(f"Key not found: {self.key_path}")

        context.load_cert_chain(
            certfile=str(cert_path),
            keyfile=str(key_path),
            password=self.key_password if self.key_password else None,
        )
        context.load_default_certs()

        return context

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the async HTTP/2 client."""
        if self._client is None:
            ssl_context = self._get_ssl_context()
            self._client = httpx.AsyncClient(
                http2=True,
                verify=ssl_context,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    async def send_update_notification(self, push_token: str) -> bool:
        """Send a push notification asynchronously."""
        url = f"https://{self._host}:{APNS_PORT}/3/device/{push_token}"

        headers = {
            "apns-topic": self.pass_type_id,
            "apns-push-type": "background",
            "apns-priority": "5",
        }

        payload = "{}"

        try:
            client = await self._get_client()
            response = await client.post(url, content=payload, headers=headers)

            if response.status_code == 200:
                logger.info(
                    "async_push_notification_sent",
                    push_token=push_token[:20] + "...",
                )
                return True

            reason = None
            try:
                error_json = response.json()
                reason = error_json.get("reason")
            except Exception:
                pass

            raise ApplePushError(
                f"APNs returned status {response.status_code}",
                status_code=response.status_code,
                reason=reason,
            )

        except httpx.RequestError as e:
            raise ApplePushError(f"Request failed: {e}")

    async def close(self) -> None:
        """Close the async client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "AsyncApplePushNotificationClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: type | None, exc_val: Exception | None, exc_tb: object) -> None:
        """Async context manager exit."""
        await self.close()
