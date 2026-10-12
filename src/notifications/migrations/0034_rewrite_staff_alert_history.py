"""Re-type historical staff alerts onto the new staff notification types (#1127).

Rows written by ``notify_org_staff`` since #1128 carry ``"staff_alert": true`` in their context
while borrowing the buyer's type. Map them to the staff type (so old alerts render in staff voice
and the staff-alert flag can go away), then strip the flag from every row. Reverse re-types the
five staff types back and re-adds the flag; the flag stripped from other types is not restored
(nothing reads it any more).
"""

from django.db import migrations

# buyer type -> staff alert type
PAIRS = [
    ("ticket_created", "staff_ticket_created"),
    ("ticket_cancelled", "staff_ticket_cancelled"),
    ("ticket_refunded", "staff_ticket_refunded"),
    ("series_pass_purchased", "staff_series_pass_purchased"),
    ("series_pass_cancelled", "staff_series_pass_cancelled"),
]

TABLE = "notifications_notification"

FORWARD = [
    f"UPDATE {TABLE} SET notification_type = '{staff}' "
    f"WHERE notification_type = '{buyer}' AND context ? 'staff_alert'"
    for buyer, staff in PAIRS
] + [f"UPDATE {TABLE} SET context = context - 'staff_alert' WHERE context ? 'staff_alert'"]

REVERSE = [
    f"UPDATE {TABLE} SET notification_type = '{buyer}', context = context || '{{\"staff_alert\": true}}'::jsonb "
    f"WHERE notification_type = '{staff}'"
    for buyer, staff in PAIRS
]


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0033_copy_staff_alert_preferences"),
    ]

    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]
