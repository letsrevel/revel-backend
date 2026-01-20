# Generated manually for model rename

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0041_event_requires_full_profile"),
        ("questionnaires", "0008_add_submission_metadata"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Step 1: Remove the old constraint
        migrations.RemoveConstraint(
            model_name="eventfeedbacksubmission",
            name="unique_feedback_per_user_event_questionnaire",
        ),
        # Step 2: Rename the model
        migrations.RenameModel(
            old_name="EventFeedbackSubmission",
            new_name="EventQuestionnaireSubmission",
        ),
        # Step 3: Add questionnaire_type field (denormalized for conditional constraint)
        # Default to "feedback" since all existing records are feedback submissions
        migrations.AddField(
            model_name="eventquestionnairesubmission",
            name="questionnaire_type",
            field=models.CharField(
                choices=[
                    ("admission", "Admission"),
                    ("membership", "Membership"),
                    ("feedback", "Feedback"),
                    ("generic", "Generic"),
                ],
                db_index=True,
                default="feedback",
                max_length=20,
            ),
            preserve_default=False,
        ),
        # Step 4: Add the new conditional constraint (only for FEEDBACK type)
        migrations.AddConstraint(
            model_name="eventquestionnairesubmission",
            constraint=models.UniqueConstraint(
                condition=models.Q(questionnaire_type="feedback"),
                fields=["event", "user", "questionnaire"],
                name="unique_feedback_questionnaire_per_user_event",
            ),
        ),
        # Step 5: Update related_names on ForeignKey fields
        migrations.AlterField(
            model_name="eventquestionnairesubmission",
            name="event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="questionnaire_submissions",
                to="events.event",
            ),
        ),
        migrations.AlterField(
            model_name="eventquestionnairesubmission",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="event_questionnaire_submissions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="eventquestionnairesubmission",
            name="questionnaire",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="event_questionnaire_submissions",
                to="questionnaires.questionnaire",
            ),
        ),
        migrations.AlterField(
            model_name="eventquestionnairesubmission",
            name="submission",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="event_questionnaire",
                to="questionnaires.questionnairesubmission",
            ),
        ),
        # Step 6: Add index on (user, event) for common query patterns
        migrations.AddIndex(
            model_name="eventquestionnairesubmission",
            index=models.Index(fields=["user", "event"], name="evtqsub_user_event_idx"),
        ),
        # Step 7: Set ordering on model
        migrations.AlterModelOptions(
            name="eventquestionnairesubmission",
            options={"ordering": ["-created_at"]},
        ),
    ]
