"""Mark existing superuser accounts as having a verified email.

Superusers are created by an operator (``createsuperuser``, ``bootstrap``, ``seed``). Older versions of
those commands did not set ``email_verified``, and sign-in now needs a verified address. Only superusers
are changed: staff and member accounts keep verifying their own address.
"""

import typing as t

from django.db import migrations

if t.TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def verify_superusers(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    RevelUser = apps.get_model("accounts", "RevelUser")
    RevelUser.objects.filter(is_superuser=True, guest=False, email_verified=False).update(email_verified=True)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0037_reveluser_signup_contested_at"),
    ]

    operations = [
        migrations.RunPython(verify_superusers, migrations.RunPython.noop),
    ]
