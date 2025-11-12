"""Channel registry for notification delivery."""

from notifications.enums import DeliveryChannel
from notifications.service.channels.base import NotificationChannel
from notifications.service.channels.email import EmailChannel
from notifications.service.channels.in_app import InAppChannel
from notifications.service.channels.telegram import TelegramChannel


class ChannelRegistry:
    """Registry for notification delivery channels."""

    def __init__(self) -> None:
        """Initialize channel registry."""
        self._channels: dict[str, NotificationChannel] = {}
        self._register_default_channels()

    def _register_default_channels(self) -> None:
        """Register all default channels."""
        self.register(InAppChannel())
        self.register(EmailChannel())
        self.register(TelegramChannel())

    def register(self, channel: NotificationChannel) -> None:
        """Register a channel.

        Args:
            channel: Channel instance to register
        """
        self._channels[channel.get_channel_name()] = channel

    def get(self, channel_name: str) -> NotificationChannel:
        """Get channel by name.

        Args:
            channel_name: Name of the channel

        Returns:
            Channel instance

        Raises:
            ValueError: If channel is not registered
        """
        channel = self._channels.get(channel_name)
        if not channel:
            raise ValueError(f"No channel registered for: {channel_name}")
        return channel

    def get_all(self) -> dict[str, NotificationChannel]:
        """Get all registered channels.

        Returns:
            Dictionary of channel_name -> channel_instance
        """
        return self._channels.copy()


# Global registry instance
_registry = ChannelRegistry()


def get_channel(channel_name: str) -> NotificationChannel:
    """Get channel from global registry.

    Args:
        channel_name: Name of the channel

    Returns:
        Channel instance
    """
    return _registry.get(channel_name)


def get_all_channels() -> dict[str, NotificationChannel]:
    """Get all channels from global registry.

    Returns:
        Dictionary of channel_name -> channel_instance
    """
    return _registry.get_all()


# Convenient mapping for task usage
CHANNEL_INSTANCES: dict[str, NotificationChannel] = {
    DeliveryChannel.IN_APP: InAppChannel(),
    DeliveryChannel.EMAIL: EmailChannel(),
    DeliveryChannel.TELEGRAM: TelegramChannel(),
}


def get_channel_instance(channel: str) -> NotificationChannel:
    """Get channel instance by name.

    Args:
        channel: Channel name

    Returns:
        Channel instance

    Raises:
        KeyError: If channel is not registered
    """
    return CHANNEL_INSTANCES[channel]
