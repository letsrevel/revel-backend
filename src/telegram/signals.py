"""Custom signals for Telegram account lifecycle events.

These signals decouple the telegram app from notifications — telegram fires the
signals, and notifications listens and reacts (e.g. enabling/disabling the
TELEGRAM delivery channel).
"""

from django.dispatch import Signal

# Fired after a user successfully links their Telegram account.
# Kwargs: user (RevelUser), telegram_user (TelegramUser)
telegram_account_linked = Signal()

# Fired after a user disconnects their Telegram account.
# Kwargs: user (RevelUser), telegram_user (TelegramUser)
telegram_account_unlinked = Signal()
