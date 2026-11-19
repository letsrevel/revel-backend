"""Base TypedDict every notification context schema extends."""

import typing as t


class BaseNotificationContext(t.TypedDict):
    """Base context for all notifications."""

    # Frontend URL for deep linking
    frontend_url: t.NotRequired[str]
