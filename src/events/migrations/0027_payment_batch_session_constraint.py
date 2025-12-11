"""Migration to support batch ticket purchases sharing the same Stripe session.

Changes:
- Removes unique constraint from stripe_session_id (was blocking batch purchases)
- Adds composite unique constraint (stripe_session_id, ticket) to prevent duplicate payments
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0026_finalize_guest_name"),
    ]

    operations = [
        # Remove the unique constraint from stripe_session_id field
        migrations.AlterField(
            model_name="payment",
            name="stripe_session_id",
            field=models.CharField(max_length=255, db_index=True),
        ),
        # Add composite unique constraint to prevent duplicate payments per ticket/session
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(
                fields=["stripe_session_id", "ticket"],
                name="unique_payment_per_session_ticket",
            ),
        ),
    ]
