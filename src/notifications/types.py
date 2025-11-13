import typing as t

from .enums import DeliveryChannel


# {notification_type: {enabled: bool, channels: []}}
class NotificationTypeSetting(t.TypedDict):
    enabled: bool
    channels: list[DeliveryChannel]
