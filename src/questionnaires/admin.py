# src/questionnaires/admin.py

import json
import typing as t

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin, StackedInline, TabularInline

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
        (None, {"fields": ("question", "section")}),
        (
            "Configuration",
            {
                "fields": (
                    "allow_multiple_answers",
                    "shuffle_options",
                    "is_mandatory",
                    "is_fatal",
                    "order",
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
    )


class FreeTextQuestionInline(StackedInline):  # type: ignore[misc]
    """Inline for Free Text Questions within a Questionnaire."""

    model = models.FreeTextQuestion
    extra = 1
    ordering = ["order"]
    classes = ["collapse"]
    fieldsets = (
        (None, {"fields": ("question", "section")}),
        (
            "Configuration",
            {
                "fields": ("is_mandatory", "is_fatal", "order"),
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
    )


class QuestionnaireSectionInline(TabularInline):  # type: ignore[misc]
    """Inline for Sections within a Questionnaire."""

    model = models.QuestionnaireSection
    extra = 1
    ordering = ["order"]


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


@admin.register(models.QuestionnaireSubmission)
class QuestionnaireSubmissionAdmin(ModelAdmin, UserLinkMixin):  # type: ignore[misc]
    """Admin model for Questionnaire Submissions."""

    list_display = ["__str__", "user_link", "questionnaire_link", "status", "submitted_at"]
    list_filter = ["status", "questionnaire__name", "created_at", "submitted_at"]
    search_fields = ["user__username", "questionnaire__name"]
    autocomplete_fields = ["user", "questionnaire"]
    readonly_fields = ["submitted_at"]
    date_hierarchy = "submitted_at"

    inlines = [
        QuestionnaireEvaluationInline,
        MultipleChoiceAnswerInline,
        FreeTextAnswerInline,
    ]

    def questionnaire_link(self, obj: models.QuestionnaireSubmission) -> str:
        url = reverse("admin:questionnaires_questionnaire_change", args=[obj.questionnaire.id])
        return format_html('<a href="{}">{}</a>', url, obj.questionnaire.name)

    questionnaire_link.short_description = "Questionnaire"  # type: ignore[attr-defined]


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
            models.QuestionnaireEvaluation.Status.APPROVED: "green",
            models.QuestionnaireEvaluation.Status.REJECTED: "red",
            models.QuestionnaireEvaluation.Status.PENDING_REVIEW: "orange",
        }
        color = colors.get(obj.status, "gray")  # type: ignore[call-overload]
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="Proposed Status")
    def proposed_status_display(self, obj: models.QuestionnaireEvaluation) -> str:
        if not obj.proposed_status:
            return "—"
        colors = {
            models.QuestionnaireEvaluation.Status.APPROVED: "green",
            models.QuestionnaireEvaluation.Status.REJECTED: "red",
            models.QuestionnaireEvaluation.Status.PENDING_REVIEW: "orange",
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
    search_fields = ["question", "questionnaire__name", "section__name"]
    autocomplete_fields = ["questionnaire", "section"]
    ordering = ["questionnaire", "section", "order"]

    inlines = [MultipleChoiceOptionInline]

    @admin.display(description="Question")
    def question_short(self, obj: models.MultipleChoiceQuestion) -> str:
        return obj.question[:100] + "..." if len(obj.question) > 100 else obj.question

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
    search_fields = ["question", "questionnaire__name", "section__name", "llm_guidelines"]
    autocomplete_fields = ["questionnaire", "section"]
    ordering = ["questionnaire", "section", "order"]

    @admin.display(description="Question")
    def question_short(self, obj: models.FreeTextQuestion) -> str:
        return obj.question[:100] + "..." if len(obj.question) > 100 else obj.question

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


@admin.register(models.QuestionnaireSection)
class QuestionnaireSectionAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for Questionnaire Sections."""

    list_display = ["name", "questionnaire_link", "order", "question_count", "created_at"]
    list_filter = ["questionnaire__name", "created_at"]
    search_fields = ["name", "questionnaire__name"]
    autocomplete_fields = ["questionnaire"]
    ordering = ["questionnaire", "order"]

    @admin.display(description="Questionnaire")
    def questionnaire_link(self, obj: models.QuestionnaireSection) -> str:
        url = reverse("admin:questionnaires_questionnaire_change", args=[obj.questionnaire.id])
        return format_html('<a href="{}">{}</a>', url, obj.questionnaire.name)

    @admin.display(description="Questions")
    def question_count(self, obj: models.QuestionnaireSection) -> int:
        mc_count = obj.multiplechoicequestion_questions.count()
        ft_count = obj.freetextquestion_questions.count()
        return mc_count + ft_count
