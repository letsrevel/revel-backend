"""Channel instances for notification delivery, keyed by ``DeliveryChannel``."""

from notifications.enums import DeliveryChannel
from notifications.service.channels.base import NotificationChannel
from notifications.service.channels.email import EmailChannel
from notifications.service.channels.in_app import InAppChannel
from notifications.service.channels.telegram import TelegramChannel

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
