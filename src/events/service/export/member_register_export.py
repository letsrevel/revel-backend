"""Generate an Excel export of an organization's member register (#1319)."""

from datetime import date
from io import BytesIO
from uuid import UUID

import structlog
from django.db import transaction
from django.utils import timezone, translation
from openpyxl import Workbook

from accounts.models import RevelUser
from common.models import FileExport
from common.service.export_service import complete_export, fail_export, start_export
from events.models import MembershipRecord, Organization
from events.utils import get_organization_day_bounds, get_organization_timezone

from .formatting import auto_fit_columns, sanitize_cell, style_header_row, style_summary_sheet

logger = structlog.get_logger(__name__)


def start_member_register_export(
    organization: Organization, requested_by: RevelUser, active_on: date | None
) -> FileExport:
    """Create a ``FileExport`` for the member register and dispatch the export task.

    Args:
        organization: The organization whose register is exported.
        requested_by: The user requesting the export.
        active_on: If given, only records overlapping this calendar day in the
            organization's timezone are exported.

    Returns:
        The newly-created ``FileExport`` in PENDING state.
    """
    from events.tasks import generate_member_register_export_task

    export = FileExport.objects.create(
        requested_by=requested_by,
        export_type=FileExport.ExportType.MEMBER_REGISTER,
        parameters={"org_id": str(organization.id), "active_on": active_on.isoformat() if active_on else None},
    )
    # Dispatch after commit so the worker can SELECT the FileExport row.
    transaction.on_commit(lambda: generate_member_register_export_task.delay(str(export.id)))
    return export


def generate_member_register_export(export_id: UUID) -> None:
    """Build an Excel workbook with the organization's member register."""
    export = FileExport.objects.select_related("requested_by").get(pk=export_id)
    start_export(export)
    try:
        file_bytes = _build_workbook(export)
        complete_export(export, file_bytes, f"member_register_{export_id}.xlsx")
        logger.info("member_register_export_completed", export_id=str(export_id))
    except Exception as e:
        fail_export(export, f"Export failed: {e}")
        logger.exception("member_register_export_failed", export_id=str(export_id))
        raise


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _build_workbook(export: FileExport) -> bytes:
    """Build the workbook and return raw bytes."""
    organization = Organization.objects.get(pk=UUID(export.parameters["org_id"]))
    raw_active_on = export.parameters.get("active_on")
    day = date.fromisoformat(raw_active_on) if raw_active_on else None

    qs = MembershipRecord.objects.filter(organization=organization).select_related("tier")
    if day is not None:
        qs = qs.active_during(*get_organization_day_bounds(organization, day))
    records = list(qs.order_by("valid_from", "created_at"))

    wb = Workbook()
    ws = wb.active if wb.active is not None else wb.create_sheet()
    ws.title = "Summary"
    open_count = sum(1 for r in records if r.valid_until is None)
    standard = sum(1 for r in records if r.kind == MembershipRecord.Kind.STANDARD)
    for row in [
        ("Organization", sanitize_cell(organization.name)),
        ("Generated at", timezone.now().isoformat()),
        ("Members on", f"{day.isoformat()} ({get_organization_timezone(organization)})" if day else "All records"),
        ("Total records", len(records)),
        ("Open records", open_count),
        ("Standard", standard),
        ("Temporary", len(records) - standard),
    ]:
        ws.append(row)
    style_summary_sheet(ws)

    _write_register_sheet(wb, records)

    buf = BytesIO()
    wb.save(buf)
    file_bytes = buf.getvalue()
    buf.close()
    wb.close()
    return file_bytes


def _write_register_sheet(wb: Workbook, records: list[MembershipRecord]) -> None:
    """Populate the Register sheet; enum labels are rendered in English."""
    ws = wb.create_sheet("Register")
    ws.append(
        [
            "Name",
            "Email",
            "Kind",
            "Tier",
            "Valid From",
            "Valid Until",
            "Ended Reason",
            "Source",
            "Approximate Dates",
            "Has Account",
            "Questionnaire",
            "Accepted At",
            "Created At",
        ]
    )
    with translation.override("en"):
        for r in records:
            ws.append(
                [
                    sanitize_cell(r.full_name),
                    sanitize_cell(r.email),
                    str(MembershipRecord.Kind(r.kind).label),
                    sanitize_cell(r.tier_name),
                    r.valid_from.isoformat(),
                    r.valid_until.isoformat() if r.valid_until else "",
                    str(MembershipRecord.EndedReason(r.ended_reason).label) if r.ended_reason else "",
                    str(MembershipRecord.Source(r.source).label),
                    _yes_no(r.source == MembershipRecord.Source.BACKFILL),
                    _yes_no(r.user_id is not None),
                    sanitize_cell(r.questionnaire_name or ""),
                    r.accepted_at.isoformat() if r.accepted_at else "",
                    r.created_at.isoformat(),
                ]
            )
    style_header_row(ws)
    auto_fit_columns(ws)
