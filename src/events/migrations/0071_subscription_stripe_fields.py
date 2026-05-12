# Generated for Phase 2: Stripe ONLINE subscriptions.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0070_organization_membership_grace_period_days_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="membershipsubscriptionplan",
            name="payment_method",
            field=models.CharField(
                choices=[("online", "Online (Stripe)"), ("offline", "Offline (staff-managed)")],
                default="offline",
                help_text="ONLINE plans are billed via Stripe; OFFLINE plans are tracked manually by staff.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="membershipsubscriptionplan",
            name="stripe_product_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="membershipsubscriptionplan",
            name="stripe_price_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="membershipsubscription",
            name="stripe_subscription_id",
            field=models.CharField(blank=True, max_length=255, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="membershippayment",
            name="stripe_invoice_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="membershippayment",
            name="stripe_payment_intent_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=255),
        ),
        migrations.CreateModel(
            name="CustomerProfile",
            fields=[
                (
                    "id",
                    models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True, db_index=True)),
                ("stripe_customer_id", models.CharField(db_index=True, max_length=255)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="customer_profiles",
                        to="events.organization",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="customer_profiles",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="customerprofile",
            constraint=models.UniqueConstraint(
                fields=("user", "organization"), name="unique_customer_per_user_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="customerprofile",
            constraint=models.UniqueConstraint(
                fields=("organization", "stripe_customer_id"),
                name="unique_stripe_customer_per_org",
            ),
        ),
    ]
