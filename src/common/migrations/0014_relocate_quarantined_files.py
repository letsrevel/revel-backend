"""Relocate existing quarantined files under the protected/ prefix.

The QuarantinedFile.file field was changed from a plain FileField (served publicly
under /media/quarantined_files/*) to a ProtectedFileField (stored under
protected/quarantined_files/* and reachable only via a signed, authorized URL).

This data migration moves any files written under the old, publicly-served path to
the protected path and updates each row's stored name, so previously-quarantined
malware/PII bytes stop being world-readable. There are typically very few rows, so
we materialize the list rather than streaming with .iterator().
"""

from django.db import migrations

_PREFIX = "protected/"


def _relocate(file_field_storage, old_name: str, new_name: str) -> str:
    """Copy old_name -> new_name in storage, returning the actually-saved name."""
    with file_field_storage.open(old_name, "rb") as fh:
        saved_name = file_field_storage.save(new_name, fh)
    file_field_storage.delete(old_name)
    return saved_name


def add_protected_prefix(apps, schema_editor):
    QuarantinedFile = apps.get_model("common", "QuarantinedFile")
    for qf in list(QuarantinedFile.objects.all()):
        name = qf.file.name
        if not name or name.startswith(_PREFIX):
            continue
        storage = qf.file.storage
        new_name = f"{_PREFIX}{name}"
        if storage.exists(name):
            qf.file.name = _relocate(storage, name, new_name)
        elif storage.exists(new_name):
            # File was already moved (e.g. partial run); just fix the pointer.
            qf.file.name = new_name
        else:
            # Underlying bytes are gone; leave the pointer untouched.
            continue
        qf.save(update_fields=["file"])


def strip_protected_prefix(apps, schema_editor):
    QuarantinedFile = apps.get_model("common", "QuarantinedFile")
    for qf in list(QuarantinedFile.objects.all()):
        name = qf.file.name
        if not name or not name.startswith(_PREFIX):
            continue
        storage = qf.file.storage
        old_name = name[len(_PREFIX) :]
        if storage.exists(name):
            qf.file.name = _relocate(storage, name, old_name)
        elif storage.exists(old_name):
            qf.file.name = old_name
        else:
            continue
        qf.save(update_fields=["file"])


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0013_alter_quarantinedfile_file"),
    ]

    operations = [
        migrations.RunPython(add_protected_prefix, strip_protected_prefix),
    ]
