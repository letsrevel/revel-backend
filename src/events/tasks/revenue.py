"""Celery tasks for revenue & VAT report generation and scheduled delivery (#551, #552).

The tasks carry explicit registered names (``events.generate_revenue_report`` and
``events.send_scheduled_revenue_reports``) so the Celery-beat schedule defined in
migration 0085 — which references the task by name string — is unaffected.
"""

from uuid import UUID

from celery import shared_task


@shared_task(name="events.generate_revenue_report")
def generate_revenue_report_task(export_id: str) -> None:
    """Generate the revenue & VAT report bundle for a FileExport (#551)."""
    from events.service.revenue_report_service import generate_revenue_report

    generate_revenue_report(UUID(export_id))


@shared_task(name="events.send_scheduled_revenue_reports")
def send_scheduled_revenue_reports_task() -> None:
    """Beat job: email just-closed-period revenue reports to opted-in orgs (#552)."""
    from django.utils import timezone

    from events.service.revenue_report_service import deliver_scheduled_revenue_reports

    deliver_scheduled_revenue_reports(timezone.now())
