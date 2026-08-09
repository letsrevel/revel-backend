"""Backfill one Refund row per Payment that already carries a refund block."""

from django.db import migrations


def backfill_refunds(apps, schema_editor):  # type: ignore[no-untyped-def]
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
