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
    """Admin model for Questionnaire Evaluations."""

    list_display = [
        "__str__",
        "submission_user",
        "status",
        "proposed_status",
        "score",
        "automatically_evaluated",
        "evaluator_link",
    ]
    list_filter = ["status", "proposed_status", "automatically_evaluated", "submission__questionnaire__name"]
    search_fields = ["submission__user__username", "submission__questionnaire__name"]
    autocomplete_fields = ["submission", "evaluator"]
    readonly_fields = ["score", "raw_evaluation_data_display", "evaluator_link"]
    date_hierarchy = "created_at"

    def submission_user(self, obj: models.QuestionnaireEvaluation) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.submission.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.submission.user.username)

    submission_user.short_description = "User"  # type: ignore[attr-defined]

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
