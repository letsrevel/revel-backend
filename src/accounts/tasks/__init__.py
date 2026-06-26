"""Tasks for the authentication app.

This package groups the accounts app's asynchronous tasks by domain (email,
tokens, gdpr, verification_reminders, notifications, bans, payouts). Every task
is re-exported here so the historical ``from accounts.tasks import <task>``
import path keeps working.

``mark_reminder_sent`` is re-exported because ``common.tasks._execute_email_callback``
resolves it via ``getattr(import_module("accounts.tasks"), "mark_reminder_sent")``
against its allowlist — it must stay importable from this package.
"""

from accounts.tasks.bans import process_domain_ban_task
from accounts.tasks.email import AccountEmail, send_account_email
from accounts.tasks.gdpr import (
    DATA_EXPORT_URL_EXPIRES_IN,
    cleanup_expired_data_exports,
    delete_user_account,
    generate_user_data_export,
)
from accounts.tasks.notifications import notify_admin_new_user_joined, notify_admin_new_user_joined_discord
from accounts.tasks.payouts import generate_and_send_payout_statement, process_referral_payouts
from accounts.tasks.tokens import flush_expired_tokens
from accounts.tasks.verification_reminders import (
    deactivate_unverified_accounts,
    delete_old_inactive_accounts,
    mark_reminder_sent,
    send_early_verification_reminders,
    send_final_verification_warnings,
)

__all__ = [
    "DATA_EXPORT_URL_EXPIRES_IN",
    "AccountEmail",
    "cleanup_expired_data_exports",
    "deactivate_unverified_accounts",
    "delete_old_inactive_accounts",
    "delete_user_account",
    "flush_expired_tokens",
    "generate_and_send_payout_statement",
    "generate_user_data_export",
    "mark_reminder_sent",
    "notify_admin_new_user_joined",
    "notify_admin_new_user_joined_discord",
    "process_domain_ban_task",
    "process_referral_payouts",
    "send_account_email",
    "send_early_verification_reminders",
    "send_final_verification_warnings",
]
