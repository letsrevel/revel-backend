"""Protocol definitions for wallet pass generators.

This module defines the abstract protocol that all wallet pass generators
(Apple, Google, etc.) must implement, enabling a pluggable architecture.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from events.models import Ticket
    from wallet.models import WalletPassRegistration


class WalletPassGenerator(Protocol):
    """Protocol for wallet pass generators.

    Implementations should handle platform-specific pass generation and
    push notification delivery.
    """

    def generate_pass(self, ticket: "Ticket") -> bytes:
        """Generate a wallet pass file for the given ticket.

        Args:
            ticket: The ticket to generate a pass for.

        Returns:
            The pass file as bytes (e.g., .pkpass for Apple).
        """
        ...

    def send_update_notification(self, registration: "WalletPassRegistration") -> bool:
        """Send a push notification to trigger pass update on device.

        Args:
            registration: The device registration to notify.

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        ...

    def get_pass_content_type(self) -> str:
        """Get the MIME content type for this pass format.

        Returns:
            The content type string (e.g., 'application/vnd.apple.pkpass').
        """
        ...

    def get_pass_file_extension(self) -> str:
        """Get the file extension for this pass format.

        Returns:
            The file extension without dot (e.g., 'pkpass').
        """
        ...
