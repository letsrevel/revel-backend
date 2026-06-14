"""Celery tasks for the events app.

Split into thematic submodules. Every task is re-exported here so that:
- ``events.tasks`` remains the single import surface (``from events.tasks import X``),
- Celery ``autodiscover_tasks()`` (which imports ``events.tasks``) loads every
  submodule, running each ``@shared_task`` decorator so the task registers.

Registered task names are unchanged by the split — tasks moved here keep their
explicit ``name=`` (including the previously-implicit ``events.tasks.<func>``
names that are now pinned explicitly), so existing Celery Beat ``PeriodicTask``
rows and in-flight broker messages keep resolving.
"""

from .admin_alerts import (
    notify_admin_new_organization_discord,
    notify_admin_new_organization_pushover,
)
from .exports import (
    generate_attendee_export_task,
    generate_questionnaire_export_task,
)
from .guests import (
    send_guest_rsvp_confirmation,
    send_guest_ticket_confirmation,
)
from .invoicing import (
    MonthlyInvoiceGenerationResult,
    deliver_attendee_credit_note_task,
    deliver_attendee_invoice_task,
    generate_attendee_credit_note_task,
    generate_attendee_invoice_task,
    generate_monthly_invoices_task,
    send_invoice_email_task,
)
from .maintenance import (
    ResetDemoDataResult,
    TicketFileCacheCleanupResult,
    cleanup_expired_payments,
    cleanup_ticket_file_cache,
    reset_demo_data,
)
from .organization import (
    send_organization_contact_email_verification,
    send_organization_contact_message_email,
)
from .recurring import (
    RecurringEventGenerationResult,
    generate_recurring_events_task,
    generate_single_series_events_task,
)
from .referral import calculate_referral_payouts
from .subscriptions import (
    SubscriptionExpiryCounters,
    expire_subscriptions_past_grace,
    send_subscription_renewal_reminders,
)
from .vat import (
    VatRevalidationResult,
    revalidate_single_vat_id_task,
    revalidate_vat_ids_task,
)
from .visibility import build_attendee_visibility_flags
from .waitlist import (
    expire_waitlist_offers_task,
    nudge_open_waitlists_task,
    process_waitlist_for_event_task,
    send_waitlist_offer_notification_task,
)

__all__ = [
    # admin_alerts
    "notify_admin_new_organization_discord",
    "notify_admin_new_organization_pushover",
    # exports
    "generate_attendee_export_task",
    "generate_questionnaire_export_task",
    # guests
    "send_guest_rsvp_confirmation",
    "send_guest_ticket_confirmation",
    # invoicing
    "MonthlyInvoiceGenerationResult",
    "deliver_attendee_credit_note_task",
    "deliver_attendee_invoice_task",
    "generate_attendee_credit_note_task",
    "generate_attendee_invoice_task",
    "generate_monthly_invoices_task",
    "send_invoice_email_task",
    # maintenance
    "ResetDemoDataResult",
    "TicketFileCacheCleanupResult",
    "cleanup_expired_payments",
    "cleanup_ticket_file_cache",
    "reset_demo_data",
    # organization
    "send_organization_contact_email_verification",
    "send_organization_contact_message_email",
    # recurring
    "RecurringEventGenerationResult",
    "generate_recurring_events_task",
    "generate_single_series_events_task",
    # referral
    "calculate_referral_payouts",
    # subscriptions
    "SubscriptionExpiryCounters",
    "expire_subscriptions_past_grace",
    "send_subscription_renewal_reminders",
    # vat
    "VatRevalidationResult",
    "revalidate_single_vat_id_task",
    "revalidate_vat_ids_task",
    # visibility
    "build_attendee_visibility_flags",
    # waitlist
    "expire_waitlist_offers_task",
    "nudge_open_waitlists_task",
    "process_waitlist_for_event_task",
    "send_waitlist_offer_notification_task",
]
