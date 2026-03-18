from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0056_bump_location_maps_url_max_length"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="billing_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Legal entity name for invoicing (e.g., company or individual name as registered)."
                    " Falls back to the organization name if empty."
                ),
                max_length=255,
            ),
        ),
    ]
