import uuid

from django.db import migrations


def backfill_reservation_id(apps, schema_editor):
    """Assign one fresh UUID per distinct stripe_session_id.

    Legacy Payment rows created before #632 group by stripe_session_id. The
    interactive cancel/resume/cleanup paths switch to reservation_id grouping,
    so in-flight PENDING batches that straddle the deploy must get a
    reservation_id that preserves their sibling grouping: rows sharing a
    session id get the same reservation_id; distinct sessions get distinct ones.
    """
    Payment = apps.get_model("events", "Payment")
    session_ids = (
        Payment.objects.filter(reservation_id__isnull=True)
        .exclude(stripe_session_id="")
        .values_list("stripe_session_id", flat=True)
        .distinct()
    )
    for session_id in session_ids:
        Payment.objects.filter(
            stripe_session_id=session_id, reservation_id__isnull=True
        ).update(reservation_id=uuid.uuid4())


def reverse(apps, schema_editor):
    # Additive/irreversible-by-design: leave reservation_id as-is on downgrade.
    pass


class Migration(migrations.Migration):
    dependencies = [("events", "0094_payment_reservation_id")]
    operations = [migrations.RunPython(backfill_reservation_id, reverse)]
