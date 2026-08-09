"""Backfill one Refund row per Payment that already carries a refund block."""

import typing as t

from django.db import migrations


def backfill_refunds(apps: t.Any, schema_editor: t.Any) -> None:
    Payment = apps.get_model("events", "Payment")
    Refund = apps.get_model("events", "Refund")

    source_by_cancellation = {
        "user": "user_cancellation",
        "organizer": "organizer_api",
        "stripe_dashboard": "stripe_dashboard",
    }
    to_create = []
    qs = Payment.objects.exclude(refund_status__isnull=True).select_related("ticket")
    for payment in qs.iterator():
        amount = payment.refund_amount if payment.refund_amount is not None else payment.amount
        source = source_by_cancellation.get(payment.ticket.cancellation_source, "stripe_dashboard")
        to_create.append(
            Refund(
                payment_id=payment.id,
                amount=amount,
                currency=payment.currency,
                status=payment.refund_status,
                stripe_refund_id=payment.stripe_refund_id or "",
                failure_reason=payment.refund_failure_reason or "",
                reason=payment.ticket.cancellation_reason or "",
                source=source,
                # Preserve period attribution for revenue reports: the legacy
                # refund date is the closest thing to a confirmation timestamp.
                succeeded_at=payment.refunded_at,
            )
        )
    Refund.objects.bulk_create(to_create, batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0112_organizer_refunds"),
    ]
    operations = [
        migrations.RunPython(backfill_refunds, migrations.RunPython.noop),
    ]
