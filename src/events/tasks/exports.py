"""Celery tasks for Excel exports (questionnaire submissions, attendees)."""

from uuid import UUID

from celery import shared_task


@shared_task(name="events.tasks.generate_questionnaire_export_task")
def generate_questionnaire_export_task(export_id: str) -> None:
    """Generate an Excel export of questionnaire submissions."""
    from events.service.export.questionnaire_export import generate_questionnaire_export

    generate_questionnaire_export(UUID(export_id))


@shared_task(name="events.tasks.generate_attendee_export_task")
def generate_attendee_export_task(export_id: str) -> None:
    """Generate an Excel export of event attendees."""
    from events.service.export.attendee_export import generate_attendee_export

    generate_attendee_export(UUID(export_id))
