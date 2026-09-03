"""Backfill ExternalIdentity rows from django-google-sso's GoogleSSOUser table.

The admin login page keeps writing GoogleSSOUser rows; this one-off copy makes the
API login path recognise users who previously signed in with Google.
"""

import typing as t

from django.db import migrations


def forwards(apps: t.Any, schema_editor: t.Any) -> None:
    GoogleSSOUser = apps.get_model("django_google_sso", "GoogleSSOUser")
    ExternalIdentity = apps.get_model("accounts", "ExternalIdentity")
    for row in GoogleSSOUser.objects.select_related("user").iterator(chunk_size=500):
        ExternalIdentity.objects.get_or_create(
            provider="google",
            subject=row.google_id,
            defaults={"user": row.user, "email": row.user.email},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0032_externalidentity"),
        ("django_google_sso", "0002_alter_googlessouser_picture_url"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
