# Generated manually to drop AccountOTP table after moving to telegram app

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0007_seed_common_food_items'),
    ]

    operations = [
        migrations.DeleteModel(
            name='AccountOTP',
        ),
    ]
