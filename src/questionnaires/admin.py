# src/questionnaires/admin.py

import json
import typing as t

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin, StackedInline, TabularInline

from common.signing import get_file_url

from . import models


# --- Helper Mixins for Reusable Link Fields ---
class UserLinkMixin:
    """Mixin to add a link to a user."""

    def user_link(self, obj: t.Any) -> str | None:
        if not hasattr(obj, "user") or not obj.user:
            return None
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    user_link.short_description = "User"  # type: ignore[attr-defined]


# --- Inlines for Questionnaire Admin ---
class MultipleChoiceOptionInline(TabularInline):  # type: ignore[misc]
    """Inline for Multiple Choice Options within a Multiple Choice Question."""

    model = models.MultipleChoiceOption
    extra = 1
    ordering = ["order"]


class MultipleChoiceQuestionInline(StackedInline):  # type: ignore[misc]
    """Inline for Multiple Choice Questions within a Questionnaire."""

    model = models.MultipleChoiceQuestion
    extra = 1
    inlines = [MultipleChoiceOptionInline]
    ordering = ["order"]
    classes = ["collapse"]
    fieldsets = (
        (None, {"fields": ("question", "hint", "section")}),
        (
            "Configuration",
            {
                "fields": (
                    "allow_multiple_answers",
                    "shuffle_options",
                    "is_mandatory",
                    "is_fatal",
                    "order",
                    "depends_on_option",
                ),
                "classes": ["collapse"],
            },
        ),
        (
            "Scoring",
            {
                "fields": ("positive_weight", "negative_weight"),
                "classes": ["collapse"],
            },
        ),
        (
            "Reviewer Notes",
            {
                "fields": ("reviewer_notes",),
                "classes": ["collapse"],
            },
        ),
    )


class FreeTextQuestionInline(StackedInline):  # type: ignore[misc]
    """Inline for Free Text Questions within a Questionnaire."""

    model = models.FreeTextQuestion
    extra = 1
    ordering = ["order"]
    classes = ["collapse"]
    fieldsets = (
        (None, {"fields": ("question", "hint", "section")}),
        (
            "Configuration",
            {
                "fields": ("is_mandatory", "is_fatal", "order", "depends_on_option"),
                "classes": ["collapse"],
            },
        ),
        (
            "Scoring & AI",
            {
                "fields": ("positive_weight", "negative_weight", "llm_guidelines"),
                "classes": ["collapse"],
            },
        ),
        (
            "Reviewer Notes",
            {
                "fields": ("reviewer_notes",),
                "classes": ["collapse"],
            },
        ),
    )


class QuestionnaireSectionInline(StackedInline):  # type: ignore[misc]
    """Inline for Sections within a Questionnaire."""

    model = models.QuestionnaireSection
    extra = 1
    ordering = ["order"]
    classes = ["collapse"]
    fields = ("name", "description", "order", "depends_on_option")


@admin.register(models.Questionnaire)
class QuestionnaireAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin model for Questionnaires."""

    list_display = ["name", "status", "evaluation_mode"]
    list_filter = ["status", "evaluation_mode", "created_at"]
    search_fields = [
        "name",
    ]

    tabs = [
        ("Settings", ["Settings"]),
        ("Content", ["Sections", "Questions"]),
    ]

    fieldsets = (
        (
            "Settings",
            {
                "fields": (
                    ("name",),
                    "description",
                    "status",
                    ("shuffle_questions", "shuffle_sections"),
                    ("max_attempts", "can_retake_after"),
                )
            },
        ),
        (
            "Evaluation",
            {
                "fields": (
                    "evaluation_mode",
                    "min_score",
                    "llm_backend",
                    "llm_guidelines",
                ),
            },
        ),
    )

    inlines = [
        QuestionnaireSectionInline,
        MultipleChoiceQuestionInline,
        FreeTextQuestionInline,
    ]


# --- Inlines for QuestionnaireSubmission Admin ---
class QuestionnaireEvaluationInline(StackedInline):  # type: ignore[misc]
    """Inline for the Evaluation within a Submission."""

    model = models.QuestionnaireEvaluation
    can_delete = False
    max_num = 1
    readonly_fields = [
        "score",
        "raw_evaluation_data_display",
        "proposed_status",
        "automatically_evaluated",
        "evaluator_link",
    ]
    fields = (
        "status",
        "comments",
        "score",
        "proposed_status",
        "automatically_evaluated",
        "evaluator_link",
        "raw_evaluation_data_display",
    )

    def evaluator_link(self, obj: models.QuestionnaireEvaluation) -> str | None:
        if not obj.evaluator:
            return None
        url = reverse("admin:accounts_reveluser_change", args=[obj.evaluator.id])
        return format_html('<a href="{}">{}</a>', url, obj.evaluator.username)

    evaluator_link.short_description = "Evaluator"  # type: ignore[attr-defined]

    def raw_evaluation_data_display(self, obj: models.QuestionnaireEvaluation) -> str:
        if not obj.raw_evaluation_data:
            return "—"
        pretty_json = json.dumps(obj.raw_evaluation_data, indent=2)
        return mark_safe(f"<pre>{pretty_json}</pre>")

    raw_evaluation_data_display.short_description = "Raw Evaluation Data"  # type: ignore[attr-defined]


class MultipleChoiceAnswerInline(TabularInline):  # type: ignore[misc]
    """Inline for Multiple Choice Answers within a Submission."""

    model = models.MultipleChoiceAnswer
    extra = 0
    readonly_fields = ["question", "option"]
    can_delete = False


class FreeTextAnswerInline(TabularInline):  # type: ignore[misc]
    """Inline for Free Text Answers within a Submission."""

    model = models.FreeTextAnswer
    extra = 0
    readonly_fields = ["question", "answer"]
    can_delete = False


class FileUploadAnswerInline(TabularInline):  # type: ignore[misc]
    """Inline for File Upload Answers within a Submission."""

    model = models.FileUploadAnswer
    extra = 0
    readonly_fields = ["question", "files_display"]
    can_delete = False
    fields = ["question", "files_display"]

    @admin.display(description="Files")
    def files_display(self, obj: models.FileUploadAnswer) -> str:
        if not (files := list(obj.files.all())):
            return "—"
        links: list[str] = []
        for f in files:
            if url := get_file_url(f.file):
                links.append(format_html('<a href="{}" target="_blank">{}</a>', url, f.original_filename))
            else:
                links.append(format_html("{}", f.original_filename))
        return mark_safe(", ".join(links))


@admin.register(models.QuestionnaireSubmission)
class QuestionnaireSubmissionAdmin(ModelAdmin, UserLinkMixin):  # type: ignore[misc]
    """Admin model for Questionnaire Submissions."""

    list_display = ["__str__", "user_link", "questionnaire_link", "status", "evaluation_status", "submitted_at"]
    list_filter = ["status", "evaluation__status", "questionnaire__name", "created_at", "submitted_at"]
    search_fields = ["user__username", "questionnaire__name"]
    autocomplete_fields = ["user", "questionnaire"]
    readonly_fields = ["submitted_at", "metadata_display"]
    date_hierarchy = "submitted_at"

    fieldsets = (
        (None, {"fields": ("user", "questionnaire", "status", "submitted_at")}),
        ("Metadata", {"fields": ("metadata_display",), "classes": ["collapse"]}),
    )

    inlines = [
        QuestionnaireEvaluationInline,
        MultipleChoiceAnswerInline,
        FreeTextAnswerInline,
        FileUploadAnswerInline,
    ]

    def get_queryset(self, request: HttpRequest) -> QuerySet[models.QuestionnaireSubmission]:
        qs: QuerySet[models.QuestionnaireSubmission] = super().get_queryset(request)
        return qs.select_related("evaluation", "questionnaire", "user")

    def questionnaire_link(self, obj: models.QuestionnaireSubmission) -> str:
        url = reverse("admin:questionnaires_questionnaire_change", args=[obj.questionnaire.id])
        return format_html('<a href="{}">{}</a>', url, obj.questionnaire.name)

    questionnaire_link.short_description = "Questionnaire"  # type: ignore[attr-defined]

    @admin.display(description="Evaluation")
    def evaluation_status(self, obj: models.QuestionnaireSubmission) -> str:
        evaluation = getattr(obj, "evaluation", None)
        if not evaluation:
            return "—"
        colors = {
            models.QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED: "green",
            models.QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED: "red",
            models.QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW: "orange",
        }
        color = colors.get(evaluation.status, "gray")
        return mark_safe(f'<span style="color: {color};">{evaluation.get_status_display()}</span>')

    def metadata_display(self, obj: models.QuestionnaireSubmission) -> str:
        if not obj.metadata:
            return "—"
        pretty_json = json.dumps(obj.metadata, indent=2)
        return mark_safe(f"<pre style='background: #f8f9fa; padding: 10px; border-radius: 4px;'>{pretty_json}</pre>")

    metadata_display.short_description = "Metadata"  # type: ignore[attr-defined]


@admin.register(models.QuestionnaireEvaluation)
class QuestionnaireEvaluationAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin model for Questionnaire Evaluations with enhanced workflow."""

    list_display = [
        "__str__",
        "submission_user",
        "questionnaire_name",
        "status_display",
        "proposed_status_display",
        "score_display",
        "automatically_evaluated_display",
        "evaluator_link",
        "created_at",
    ]
    list_filter = [
        "status",
        "proposed_status",
        "automatically_evaluated",
        "submission__questionnaire__name",
        "submission__questionnaire__evaluation_mode",
        "created_at",
    ]
    search_fields = ["submission__user__username", "submission__questionnaire__name", "comments"]
    autocomplete_fields = ["submission", "evaluator"]
    readonly_fields = [
        "score",
        "raw_evaluation_data_display",
        "evaluator_link",
        "created_at",
        "updated_at",
        "submission_link",
        "evaluation_workflow_display",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = (
        (
            "Evaluation Details",
            {
                "fields": (
                    "submission_link",
                    ("status", "proposed_status"),
                    ("score", "automatically_evaluated"),
                    "evaluator_link",
                    "comments",
                )
            },
        ),
        (
            "Workflow Information",
            {
                "fields": (
                    "evaluation_workflow_display",
                    "raw_evaluation_data_display",
                ),
                "classes": ["collapse"],
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ["collapse"]}),
    )

    def submission_user(self, obj: models.QuestionnaireEvaluation) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.submission.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.submission.user.username)

    submission_user.short_description = "User"  # type: ignore[attr-defined]
    submission_user.admin_order_field = "submission__user__username"  # type: ignore[attr-defined]

    @admin.display(description="Questionnaire")
    def questionnaire_name(self, obj: models.QuestionnaireEvaluation) -> str:
        url = reverse("admin:questionnaires_questionnaire_change", args=[obj.submission.questionnaire.id])
        return format_html('<a href="{}">{}</a>', url, obj.submission.questionnaire.name)

    @admin.display(description="Status")
    def status_display(self, obj: models.QuestionnaireEvaluation) -> str:
        colors = {
            models.QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED: "green",
            models.QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED: "red",
            models.QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW: "orange",
        }
        color = colors.get(obj.status, "gray")  # type: ignore[call-overload]
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="Proposed Status")
    def proposed_status_display(self, obj: models.QuestionnaireEvaluation) -> str:
        if not obj.proposed_status:
            return "—"
        colors = {
            models.QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED: "green",
            models.QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED: "red",
            models.QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW: "orange",
        }
        color = colors.get(obj.proposed_status, "gray")  # type: ignore[call-overload]
        return mark_safe(f'<span style="color: {color};">{obj.get_proposed_status_display()}</span>')

    @admin.display(description="Score")
    def score_display(self, obj: models.QuestionnaireEvaluation) -> str:
        if obj.score is not None:
            min_score = obj.submission.questionnaire.min_score
            color = "green" if obj.score >= min_score else "red"
            return mark_safe(f'<span style="color: {color};">{obj.score:.1f} / 100</span>')
        return "—"

    @admin.display(description="Auto-Evaluated", boolean=True)
    def automatically_evaluated_display(self, obj: models.QuestionnaireEvaluation) -> bool:
        return obj.automatically_evaluated

    def evaluator_link(self, obj: models.QuestionnaireEvaluation) -> str | None:
        if not obj.evaluator:
            return "System" if obj.automatically_evaluated else "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.evaluator.id])
        return format_html('<a href="{}">{}</a>', url, obj.evaluator.username)

    evaluator_link.short_description = "Evaluator"  # type: ignore[attr-defined]

    def submission_link(self, obj: models.QuestionnaireEvaluation) -> str:
        url = reverse("admin:questionnaires_questionnairesubmission_change", args=[obj.submission.id])
        return format_html('<a href="{}">{}</a>', url, f"Submission by {obj.submission.user.username}")

    submission_link.short_description = "Submission"  # type: ignore[attr-defined]

    def evaluation_workflow_display(self, obj: models.QuestionnaireEvaluation) -> str:
        html = "<h4>Evaluation Workflow:</h4><ul>"

        html += f"<li><strong>Questionnaire:</strong> {obj.submission.questionnaire.name}</li>"
        html += (
            f"<li><strong>Evaluation Mode:</strong> {obj.submission.questionnaire.get_evaluation_mode_display()}</li>"
        )
        html += f"<li><strong>Min Score Required:</strong> {obj.submission.questionnaire.min_score}</li>"

        if obj.automatically_evaluated:
            html += f"<li><strong>LLM Backend:</strong> {obj.submission.questionnaire.get_llm_backend_display()}</li>"

        html += f"<li><strong>Created:</strong> {obj.created_at}</li>"
        html += f"<li><strong>Last Updated:</strong> {obj.updated_at}</li>"

        html += "</ul>"
        return mark_safe(html)

    evaluation_workflow_display.short_description = "Workflow Info"  # type: ignore[attr-defined]

    def raw_evaluation_data_display(self, obj: models.QuestionnaireEvaluation) -> str:
        if not obj.raw_evaluation_data:
            return "—"
        pretty_json = json.dumps(obj.raw_evaluation_data, indent=2)
        return mark_safe(f"<pre style='background: #f8f9fa; padding: 10px; border-radius: 4px;'>{pretty_json}</pre>")

    raw_evaluation_data_display.short_description = "Raw Evaluation Data"  # type: ignore[attr-defined]


# --- Additional Model Admins for Enhanced Questionnaire Management ---


@admin.register(models.MultipleChoiceOption)
class MultipleChoiceOptionAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for Multiple Choice Options."""

    list_display = ["option", "question_link", "is_correct", "order"]
    list_filter = ["is_correct", "question__questionnaire__name"]
    search_fields = ["option", "question__question", "question__questionnaire__name"]
    autocomplete_fields = ["question"]
    ordering = ["question", "order"]

    @admin.display(description="Question")
    def question_link(self, obj: models.MultipleChoiceOption) -> str:
        url = reverse("admin:questionnaires_multiplechoicequestion_change", args=[obj.question.id])
        question_text = obj.question.question or ""
        short = question_text[:50] + "..." if len(question_text) > 50 else question_text
        return format_html('<a href="{}">{}</a>', url, short)


@admin.register(models.MultipleChoiceQuestion)
class MultipleChoiceQuestionAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for Multiple Choice Questions."""

    list_display = [
        "question_short",
        "questionnaire_link",
        "section_link",
        "is_mandatory",
        "is_fatal",
        "allow_multiple_answers",
        "order",
    ]
    list_filter = ["questionnaire__name", "section__name", "is_mandatory", "is_fatal", "allow_multiple_answers"]
    search_fields = ["question", "hint", "reviewer_notes", "questionnaire__name", "section__name"]
    autocomplete_fields = ["questionnaire", "section", "depends_on_option"]
    ordering = ["questionnaire", "section", "order"]

    inlines = [MultipleChoiceOptionInline]

    fieldsets = (
        (None, {"fields": ("questionnaire", "section", "question", "hint")}),
        (
            "Configuration",
            {
                "fields": (
                    "allow_multiple_answers",
                    "shuffle_options",
                    "is_mandatory",
                    "is_fatal",
                    "order",
                    "depends_on_option",
                ),
            },
        ),
        ("Scoring", {"fields": ("positive_weight", "negative_weight")}),
        ("Reviewer Notes", {"fields": ("reviewer_notes",), "classes": ["collapse"]}),
    )

    @admin.display(description="Question")
    def question_short(self, obj: models.MultipleChoiceQuestion) -> str:
        question = obj.question or ""
        return question[:100] + "..." if len(question) > 100 else question

    @admin.display(description="Questionnaire")
    def questionnaire_link(self, obj: models.MultipleChoiceQuestion) -> str:
        url = reverse("admin:questionnaires_questionnaire_change", args=[obj.questionnaire.id])
        return format_html('<a href="{}">{}</a>', url, obj.questionnaire.name)

    @admin.display(description="Section")
    def section_link(self, obj: models.MultipleChoiceQuestion) -> str | None:
        if not obj.section:
            return "—"
        return obj.section.name


@admin.register(models.FreeTextQuestion)
class FreeTextQuestionAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for Free Text Questions."""

    list_display = [
        "question_short",
        "questionnaire_link",
        "section_link",
        "is_mandatory",
        "is_fatal",
        "has_llm_guidelines",
        "order",
    ]
    list_filter = ["questionnaire__name", "section__name", "is_mandatory", "is_fatal"]
    search_fields = ["question", "hint", "reviewer_notes", "questionnaire__name", "section__name", "llm_guidelines"]
    autocomplete_fields = ["questionnaire", "section", "depends_on_option"]
    ordering = ["questionnaire", "section", "order"]

    fieldsets = (
        (None, {"fields": ("questionnaire", "section", "question", "hint")}),
        (
            "Configuration",
            {
                "fields": ("is_mandatory", "is_fatal", "order", "depends_on_option"),
            },
        ),
        ("Scoring & AI", {"fields": ("positive_weight", "negative_weight", "llm_guidelines")}),
        ("Reviewer Notes", {"fields": ("reviewer_notes",), "classes": ["collapse"]}),
    )

    @admin.display(description="Question")
    def question_short(self, obj: models.FreeTextQuestion) -> str:
        question = obj.question or ""
        return question[:100] + "..." if len(question) > 100 else question

    @admin.display(description="Questionnaire")
    def questionnaire_link(self, obj: models.FreeTextQuestion) -> str:
        url = reverse("admin:questionnaires_questionnaire_change", args=[obj.questionnaire.id])
        return format_html('<a href="{}">{}</a>', url, obj.questionnaire.name)

    @admin.display(description="Section")
    def section_link(self, obj: models.FreeTextQuestion) -> str | None:
        if not obj.section:
            return "—"
        return obj.section.name

    @admin.display(description="Has LLM Guidelines", boolean=True)
    def has_llm_guidelines(self, obj: models.FreeTextQuestion) -> bool:
        return bool(obj.llm_guidelines)


@admin.register(models.FileUploadQuestion)
class FileUploadQuestionAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for File Upload Questions."""

    list_display = [
        "question_short",
        "questionnaire_link",
        "section_link",
        "is_mandatory",
        "max_files",
        "max_file_size_display",
        "order",
    ]
    list_filter = ["questionnaire__name", "section__name", "is_mandatory"]
    search_fields = ["question", "hint", "reviewer_notes", "questionnaire__name", "section__name"]
    autocomplete_fields = ["questionnaire", "section", "depends_on_option"]
    ordering = ["questionnaire", "section", "order"]

    fieldsets = (
        (None, {"fields": ("questionnaire", "section", "question", "hint")}),
        (
            "Configuration",
            {
                "fields": ("is_mandatory", "order", "depends_on_option"),
            },
        ),
        (
            "File Settings",
            {
                "fields": ("max_files", "max_file_size", "allowed_mime_types"),
            },
        ),
        ("Scoring", {"fields": ("positive_weight", "negative_weight")}),
        ("Reviewer Notes", {"fields": ("reviewer_notes",), "classes": ["collapse"]}),
    )

    @admin.display(description="Question")
    def question_short(self, obj: models.FileUploadQuestion) -> str:
        question = obj.question or ""
        return question[:50] + "..." if len(question) > 50 else question

    @admin.display(description="Questionnaire")
    def questionnaire_link(self, obj: models.FileUploadQuestion) -> str:
        url = reverse("admin:questionnaires_questionnaire_change", args=[obj.questionnaire.id])
        return format_html('<a href="{}">{}</a>', url, obj.questionnaire.name)

    @admin.display(description="Section")
    def section_link(self, obj: models.FileUploadQuestion) -> str | None:
        if not obj.section:
            return None
        url = reverse("admin:questionnaires_questionnairesection_change", args=[obj.section.id])
        return format_html('<a href="{}">{}</a>', url, obj.section.name)

    @admin.display(description="Max Size")
    def max_file_size_display(self, obj: models.FileUploadQuestion) -> str:
        size_mb = obj.max_file_size / (1024 * 1024)
        return f"{size_mb:.1f} MB"


@admin.register(models.QuestionnaireSection)
class QuestionnaireSectionAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for Questionnaire Sections."""

    list_display = ["name", "questionnaire_link", "order", "question_count", "created_at"]
    list_filter = ["questionnaire__name", "created_at"]
    search_fields = ["name", "description", "questionnaire__name"]
    autocomplete_fields = ["questionnaire", "depends_on_option"]
    ordering = ["questionnaire", "order"]
    fields = ["questionnaire", "name", "description", "order", "depends_on_option"]

    @admin.display(description="Questionnaire")
    def questionnaire_link(self, obj: models.QuestionnaireSection) -> str:
        url = reverse("admin:questionnaires_questionnaire_change", args=[obj.questionnaire.id])
        return format_html('<a href="{}">{}</a>', url, obj.questionnaire.name)

    @admin.display(description="Questions")
    def question_count(self, obj: models.QuestionnaireSection) -> int:
        mc_count = obj.multiplechoicequestion_questions.count()
        ft_count = obj.freetextquestion_questions.count()
        fu_count = obj.fileuploadquestion_questions.count()
        return mc_count + ft_count + fu_count


@admin.register(models.QuestionnaireFile)
class QuestionnaireFileAdmin(ModelAdmin, UserLinkMixin):  # type: ignore[misc]
    """Admin for user-uploaded questionnaire files."""

    list_display = [
        "original_filename",
        "user_link",
        "mime_type",
        "file_size_display",
        "created_at",
    ]
    list_filter = ["mime_type", "created_at"]
    search_fields = ["original_filename", "uploader__username", "uploader__email", "file_hash"]
    autocomplete_fields = ["uploader"]
    readonly_fields = [
        "file_hash",
        "file_size",
        "thumbnail_preview",
        "preview_image",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = (
        (None, {"fields": ("uploader", "original_filename", "mime_type", "file")}),
        ("File Details", {"fields": ("file_hash", "file_size")}),
        ("Previews", {"fields": ("thumbnail_preview", "preview_image"), "classes": ["collapse"]}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ["collapse"]}),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[models.QuestionnaireFile]:
        qs: QuerySet[models.QuestionnaireFile] = super().get_queryset(request)
        return qs.select_related("uploader")

    @admin.display(description="Size")
    def file_size_display(self, obj: models.QuestionnaireFile) -> str:
        size_kb = obj.file_size / 1024
        if size_kb > 1024:
            return f"{size_kb / 1024:.1f} MB"
        return f"{size_kb:.1f} KB"

    @admin.display(description="Thumbnail")
    def thumbnail_preview(self, obj: models.QuestionnaireFile) -> str:
        if url := get_file_url(obj.thumbnail):
            return format_html('<img src="{}" style="max-height: 150px;" />', url)
        return "—"

    @admin.display(description="Preview")
    def preview_image(self, obj: models.QuestionnaireFile) -> str:
        if url := get_file_url(obj.preview):
            return format_html('<img src="{}" style="max-height: 300px;" />', url)
        return "—"
