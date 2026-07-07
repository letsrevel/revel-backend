"""Celery tasks for event management.

This package groups the event app's asynchronous tasks by domain
(attendees, payments, invoicing, recurrence, subscriptions, waitlist,
exports, organization). Every task is re-exported here so the historical
``from events.tasks import <task>`` import path keeps working.
"""

from events.tasks.announcements import resend_announcements_to_new_signups, send_scheduled_announcements
from events.tasks.attendees import (
    build_attendee_visibility_flags,
    send_guest_rsvp_confirmation,
    send_guest_ticket_confirmation,
)
from events.tasks.exports import generate_attendee_export_task, generate_questionnaire_export_task
from events.tasks.invoicing import (
    calculate_referral_payouts,
    deliver_attendee_credit_note_task,
    deliver_attendee_invoice_task,
    generate_attendee_credit_note_task,
    generate_attendee_invoice_task,
    generate_monthly_invoices_task,
    redispatch_undelivered_invoices_task,
    revalidate_single_vat_id_task,
    revalidate_vat_ids_task,
    send_invoice_email_task,
)
from events.tasks.organization import (
    notify_admin_new_organization_discord,
    notify_admin_new_organization_pushover,
    reset_demo_data,
    send_organization_contact_email_verification,
    send_organization_contact_message_email,
)
from events.tasks.payments import cleanup_expired_payments, cleanup_ticket_file_cache
from events.tasks.recurrence import generate_recurring_events_task, generate_single_series_events_task
from events.tasks.revenue import generate_revenue_report_task, send_scheduled_revenue_reports_task
from events.tasks.series_pass import materialize_series_pass_holders
from events.tasks.stripe_webhooks import prune_stripe_webhook_events
from events.tasks.subscriptions import expire_subscriptions_past_grace
from events.tasks.waitlist import (
    expire_waitlist_offers_task,
    nudge_open_waitlists_task,
    process_waitlist_for_event_task,
    send_waitlist_offer_notification_task,
)

__all__ = [
    "build_attendee_visibility_flags",
    "calculate_referral_payouts",
    "cleanup_expired_payments",
    "cleanup_ticket_file_cache",
    "deliver_attendee_credit_note_task",
    "deliver_attendee_invoice_task",
    "expire_subscriptions_past_grace",
    "expire_waitlist_offers_task",
    "generate_attendee_credit_note_task",
    "generate_attendee_export_task",
    "generate_attendee_invoice_task",
    "generate_monthly_invoices_task",
    "generate_questionnaire_export_task",
    "generate_recurring_events_task",
    "generate_revenue_report_task",
    "generate_single_series_events_task",
    "materialize_series_pass_holders",
    "notify_admin_new_organization_discord",
    "notify_admin_new_organization_pushover",
    "nudge_open_waitlists_task",
    "process_waitlist_for_event_task",
    "prune_stripe_webhook_events",
    "redispatch_undelivered_invoices_task",
    "resend_announcements_to_new_signups",
    "reset_demo_data",
    "revalidate_single_vat_id_task",
    "revalidate_vat_ids_task",
    "send_guest_rsvp_confirmation",
    "send_guest_ticket_confirmation",
    "send_invoice_email_task",
    "send_organization_contact_email_verification",
    "send_organization_contact_message_email",
    "send_scheduled_announcements",
    "send_scheduled_revenue_reports_task",
    "send_waitlist_offer_notification_task",
]
