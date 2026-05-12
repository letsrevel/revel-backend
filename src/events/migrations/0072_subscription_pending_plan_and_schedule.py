# Generated for Phase 3: subscription lifecycle management.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0071_subscription_stripe_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="membershipsubscription",
            name="pending_plan",
            field=models.ForeignKey(
                blank=True,
                help_text="Plan that will replace ``plan`` at the next renewal (downgrade flow).",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="pending_subscriptions",
                to="events.membershipsubscriptionplan",
            ),
        ),
        migrations.AddField(
            model_name="membershipsubscription",
            name="stripe_schedule_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=255),
        ),
    ]
