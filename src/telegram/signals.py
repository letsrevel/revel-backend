# src/telegram/signals.py

# This file previously contained EventInvitation signal handlers that sent
# Telegram messages directly, bypassing the notifications system.
#
# These have been migrated to use the notification_requested signal instead,
# which allows for:
# - Consistent notification delivery across all channels (email, telegram, in-app)
# - User notification preferences to be respected
# - Unified tracking and logging
#
# See events/signals.py:handle_invitation_save for the new implementation.
