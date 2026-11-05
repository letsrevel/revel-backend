import typing as t

from django.db import migrations


def forward(apps: t.Any, schema_editor: t.Any) -> None:
    """Keep existing one-shot membership questionnaires one-shot.

    Previously an empty ``can_retake_after`` on a MEMBERSHIP questionnaire meant "never
    retake". It now means "retake immediately", with ``max_attempts`` (0 = unlimited) as the
    only cap. Questionnaires that relied on the old behaviour get ``max_attempts=1`` so
    their semantics do not change. Admission questionnaires are untouched.
    """
    Questionnaire = apps.get_model("questionnaires", "Questionnaire")
    Questionnaire.objects.filter(
        can_retake_after__isnull=True,
        max_attempts=0,
        org_questionnaires__questionnaire_type="membership",
    ).update(max_attempts=1)


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0137_en16931_invoicing"),
        ("questionnaires", "0014_alter_can_retake_after_help_text_retake_now"),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
