"""Generate Excel export of questionnaire submissions."""

import typing as t
from io import BytesIO
from uuid import UUID

import structlog
from django.db.models import Count, Q, QuerySet
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from common.models import FileExport
from common.service.export_service import complete_export, fail_export, notify_export_ready, start_export
from events.models import Event, EventQuestionnaireSubmission
from questionnaires.models import (
    FileUploadQuestion,
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
)

logger = structlog.get_logger(__name__)


class QuestionColumn(t.NamedTuple):
    """A column descriptor for a question in the export."""

    question_type: str
    question_id: UUID
    header_text: str
    section_order: int
    question_order: int


def generate_questionnaire_export(export_id: UUID) -> None:
    """Build an Excel workbook for questionnaire submissions and save to the FileExport."""
    export = FileExport.objects.select_related("requested_by").get(pk=export_id)
    start_export(export)
    try:
        questionnaire_id = UUID(export.parameters["questionnaire_id"])
        event_id = _optional_uuid(export.parameters.get("event_id"))
        event_series_id = _optional_uuid(export.parameters.get("event_series_id"))

        base_qs = _build_submission_queryset(questionnaire_id, event_id, event_series_id)

        submissions = list(
            base_qs.select_related("user", "evaluation").prefetch_related(
                "multiplechoiceanswer_answers__question",
                "multiplechoiceanswer_answers__option",
                "freetextanswer_answers__question",
                "fileuploadanswer_answers__question",
                "fileuploadanswer_answers__files",
            )
        )

        all_questions, mc_options = _load_question_columns(questionnaire_id)

        wb = Workbook()
        _write_summary_sheet(wb, base_qs, submissions)
        _write_submissions_sheet(wb, submissions, all_questions, mc_options)

        buf = BytesIO()
        wb.save(buf)
        file_bytes = buf.getvalue()
        buf.close()
        wb.close()
        complete_export(export, file_bytes, f"questionnaire_export_{export_id}.xlsx")

        notify_export_ready(export)
        logger.info("questionnaire_export_completed", export_id=str(export_id))

    except Exception as e:
        fail_export(export, f"Export failed: {e}")
        logger.exception("questionnaire_export_failed", export_id=str(export_id))
        raise


def _build_submission_queryset(
    questionnaire_id: UUID,
    event_id: UUID | None,
    event_series_id: UUID | None,
) -> QuerySet[QuestionnaireSubmission]:
    """Build the filtered submission queryset."""
    base_qs = QuestionnaireSubmission.objects.filter(
        questionnaire_id=questionnaire_id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )
    if event_id is not None:
        sub_ids = EventQuestionnaireSubmission.objects.filter(
            questionnaire_id=questionnaire_id, event_id=event_id
        ).values("submission_id")
        base_qs = base_qs.filter(id__in=sub_ids)
    elif event_series_id is not None:
        evt_ids = Event.objects.filter(event_series_id=event_series_id).values("id")
        sub_ids = EventQuestionnaireSubmission.objects.filter(
            questionnaire_id=questionnaire_id, event_id__in=evt_ids
        ).values("submission_id")
        base_qs = base_qs.filter(id__in=sub_ids)
    return base_qs


def _question_header(question_text: str | None, prefix: str) -> str:
    """Build a column header from a question's text."""
    text = question_text or ""
    return f"[{prefix}] {text[:80]}"


def _load_question_columns(questionnaire_id: UUID) -> tuple[list[QuestionColumn], dict[UUID, str]]:
    """Load ordered questions and MC option labels, interleaved by section/question order."""
    mc_questions = list(
        MultipleChoiceQuestion.objects.filter(questionnaire_id=questionnaire_id)
        .select_related("section")
        .order_by("section__order", "order")
    )
    ft_questions = list(
        FreeTextQuestion.objects.filter(questionnaire_id=questionnaire_id)
        .select_related("section")
        .order_by("section__order", "order")
    )
    fu_questions = list(
        FileUploadQuestion.objects.filter(questionnaire_id=questionnaire_id)
        .select_related("section")
        .order_by("section__order", "order")
    )

    all_questions: list[QuestionColumn] = []
    for mc_q in mc_questions:
        sec_order = mc_q.section.order if mc_q.section else 0
        all_questions.append(
            QuestionColumn("mc", mc_q.id, _question_header(mc_q.question, "MC"), sec_order, mc_q.order)
        )
    for ft_q in ft_questions:
        sec_order = ft_q.section.order if ft_q.section else 0
        all_questions.append(
            QuestionColumn("ft", ft_q.id, _question_header(ft_q.question, "FT"), sec_order, ft_q.order)
        )
    for fu_q in fu_questions:
        sec_order = fu_q.section.order if fu_q.section else 0
        all_questions.append(
            QuestionColumn("fu", fu_q.id, _question_header(fu_q.question, "FU"), sec_order, fu_q.order)
        )

    all_questions.sort(key=lambda q: (q.section_order, q.question_order))

    mc_options = {o.id: o.option for o in MultipleChoiceOption.objects.filter(question__in=mc_questions)}
    return all_questions, mc_options


def _write_summary_sheet(
    wb: Workbook,
    base_qs: QuerySet[QuestionnaireSubmission],
    submissions: list[QuestionnaireSubmission],
) -> None:
    """Populate the Summary sheet."""
    ws: Worksheet = wb.active  # type: ignore[assignment]
    ws.title = "Summary"

    EvalStatus = QuestionnaireEvaluation.QuestionnaireEvaluationStatus
    stats = base_qs.aggregate(
        total=Count("id"),
        unique_users=Count("user_id", distinct=True),
        approved=Count("id", filter=Q(evaluation__status=EvalStatus.APPROVED)),
        rejected=Count("id", filter=Q(evaluation__status=EvalStatus.REJECTED)),
        pending_review=Count("id", filter=Q(evaluation__status=EvalStatus.PENDING_REVIEW)),
        not_evaluated=Count("id", filter=Q(evaluation__isnull=True)),
    )

    scores = [
        float(eval_obj.score)
        for s in submissions
        if (eval_obj := getattr(s, "evaluation", None)) is not None and eval_obj.score is not None
    ]
    avg_score = sum(scores) / len(scores) if scores else None
    min_score = min(scores) if scores else None
    max_score = max(scores) if scores else None

    for label, value in [
        ("Total submissions", stats["total"]),
        ("Unique users", stats["unique_users"]),
        ("Approved", stats["approved"]),
        ("Rejected", stats["rejected"]),
        ("Pending review", stats["pending_review"]),
        ("Not evaluated", stats["not_evaluated"]),
        ("Average score", round(avg_score, 2) if avg_score is not None else "N/A"),
        ("Min score", round(min_score, 2) if min_score is not None else "N/A"),
        ("Max score", round(max_score, 2) if max_score is not None else "N/A"),
    ]:
        ws.append((label, value))


def _write_submissions_sheet(
    wb: Workbook,
    submissions: list[QuestionnaireSubmission],
    all_questions: list[QuestionColumn],
    mc_options: dict[UUID, str],
) -> None:
    """Populate the Submissions sheet."""
    ws = wb.create_sheet("Submissions")
    headers = [
        "User Email",
        "User Name",
        "Submitted At",
        "Eval Status",
        "Score",
        "Evaluator Comments",
        "Source Event",
    ] + [q.header_text for q in all_questions]
    ws.append(headers)

    for sub in submissions:
        eval_obj = getattr(sub, "evaluation", None)

        mc_answers: dict[UUID, list[str]] = {}
        for mc_ans in sub.multiplechoiceanswer_answers.all():
            mc_answers.setdefault(mc_ans.question_id, []).append(
                mc_options.get(mc_ans.option_id, str(mc_ans.option_id))
            )

        ft_answers: dict[UUID, str] = {}
        for ft_ans in sub.freetextanswer_answers.all():
            ft_answers[ft_ans.question_id] = ft_ans.answer

        fu_answers: dict[UUID, list[str]] = {}
        for fu_ans in sub.fileuploadanswer_answers.all():
            fu_answers[fu_ans.question_id] = [f.original_filename for f in fu_ans.files.all()]

        source_event = sub.source_event
        row: list[t.Any] = [
            sub.user.email if sub.user else "",
            sub.user.get_full_name() if sub.user else "",
            sub.submitted_at.isoformat() if sub.submitted_at else "",
            eval_obj.status if eval_obj else "not evaluated",
            float(eval_obj.score) if eval_obj and eval_obj.score is not None else "",
            eval_obj.comments if eval_obj and eval_obj.comments else "",
            source_event["event_name"] if source_event else "",
        ]

        for q in all_questions:
            if q.question_type == "mc":
                row.append("; ".join(mc_answers.get(q.question_id, [])))
            elif q.question_type == "ft":
                row.append(ft_answers.get(q.question_id, ""))
            else:
                row.append("; ".join(fu_answers.get(q.question_id, [])))

        ws.append(row)


def _optional_uuid(value: t.Any) -> UUID | None:
    if value is None:
        return None
    return UUID(str(value))
