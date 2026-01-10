import random
import typing as t
from uuid import UUID

from django.db import transaction
from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from accounts.models import RevelUser
from questionnaires.models import (
    FreeTextAnswer,
    FreeTextQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSection,
    QuestionnaireSubmission,
    SubmissionSourceEventMetadata,
)

from .exceptions import (
    CrossQuestionnaireSubmissionError,
    MissingMandatoryAnswerError,
    QuestionIntegrityError,
    SectionIntegrityError,
)
from .schema import (
    EvaluationCreateSchema,
    FreeTextQuestionCreateSchema,
    FreeTextQuestionSchema,
    FreeTextQuestionUpdateSchema,
    MultipleChoiceOptionCreateSchema,
    MultipleChoiceOptionSchema,
    MultipleChoiceOptionUpdateSchema,
    MultipleChoiceQuestionCreateSchema,
    MultipleChoiceQuestionSchema,
    MultipleChoiceQuestionUpdateSchema,
    QuestionnaireCreateSchema,
    QuestionnaireSchema,
    QuestionnaireSubmissionSchema,
    SectionCreateSchema,
    SectionSchema,
    SectionUpdateSchema,
)

QuestionSchemaUnion = MultipleChoiceQuestionSchema | FreeTextQuestionSchema


class QuestionnaireService:
    def __init__(self, questionnaire_id: UUID) -> None:
        """Initialize questionnaire service."""
        section_queryset = QuestionnaireSection.objects.prefetch_related(
            Prefetch(
                "multiplechoicequestion_questions",
                queryset=MultipleChoiceQuestion.objects.prefetch_related("options"),
            ),
            Prefetch(
                "freetextquestion_questions",
                queryset=FreeTextQuestion.objects.all(),
            ),
        )

        self.questionnaire = Questionnaire.objects.prefetch_related(
            Prefetch(
                "multiplechoicequestion_questions",
                queryset=MultipleChoiceQuestion.objects.filter(section__isnull=True).prefetch_related("options"),
            ),
            Prefetch(
                "freetextquestion_questions",
                queryset=FreeTextQuestion.objects.filter(section__isnull=True),
            ),
            Prefetch("sections", queryset=section_queryset),
        ).get(id=questionnaire_id)

    def build(self) -> QuestionnaireSchema:
        """Build questionnaire schema."""
        q = self.questionnaire

        def build_mc_question(mcq: MultipleChoiceQuestion) -> MultipleChoiceQuestionSchema:
            options = list(mcq.options.all())
            if mcq.shuffle_options:
                random.shuffle(options)
            else:
                options.sort(key=lambda o: o.order)

            return MultipleChoiceQuestionSchema(
                id=mcq.id,
                question=mcq.question or "",  # question is required, but MarkdownField types as str | None
                hint=mcq.hint,
                is_mandatory=mcq.is_mandatory,
                order=mcq.order,
                allow_multiple_answers=mcq.allow_multiple_answers,
                options=[MultipleChoiceOptionSchema.from_orm(opt) for opt in options],
                depends_on_option_id=mcq.depends_on_option_id,
            )

        def build_ft_question(ftq: FreeTextQuestion) -> FreeTextQuestionSchema:
            return FreeTextQuestionSchema.from_orm(ftq)

        def build_question_block(
            _mcqs: list[MultipleChoiceQuestion], _ftqs: list[FreeTextQuestion]
        ) -> tuple[list[MultipleChoiceQuestionSchema], list[FreeTextQuestionSchema]]:
            if q.shuffle_questions:
                combined = [(0, mcq) for mcq in _mcqs] + [(1, ftq) for ftq in _ftqs]
                random.shuffle(combined)
                _mcqs = [obj for kind, obj in combined if kind == 0]  # type: ignore[misc]
                _ftqs = [obj for kind, obj in combined if kind == 1]  # type: ignore[misc]
            else:
                _mcqs.sort(key=lambda x: x.order)
                _ftqs.sort(key=lambda x: x.order)

            return (
                [build_mc_question(mcq) for mcq in mcqs],
                [build_ft_question(ftq) for ftq in ftqs],
            )

        # Build sections
        sections = list(q.sections.all())
        if q.shuffle_sections:
            random.shuffle(sections)
        else:
            sections.sort(key=lambda s: s.order)

        section_schemas = []
        for section in sections:
            mcqs = list(section.multiplechoicequestion_questions.all())
            ftqs = list(section.freetextquestion_questions.all())
            mcq_schemas, ftq_schemas = build_question_block(mcqs, ftqs)

            section_schemas.append(
                SectionSchema(
                    id=section.id,
                    name=section.name,
                    description=section.description,
                    order=section.order,
                    multiple_choice_questions=mcq_schemas,
                    free_text_questions=ftq_schemas,
                    depends_on_option_id=section.depends_on_option_id,
                )
            )

        # Build top-level (unsectioned) questions
        mcqs = list(q.multiplechoicequestion_questions.all())
        ftqs = list(q.freetextquestion_questions.all())
        mcq_schemas, ftq_schemas = build_question_block(mcqs, ftqs)

        return QuestionnaireSchema(
            id=q.id,
            name=q.name,
            description=q.description,
            multiple_choice_questions=mcq_schemas,
            free_text_questions=ftq_schemas,
            sections=section_schemas,
            evaluation_mode=q.evaluation_mode,  # type: ignore[arg-type]
        )

    def _get_applicable_question_ids(
        self,
        mc_questions: dict[UUID, MultipleChoiceQuestion],
        ft_questions: dict[UUID, FreeTextQuestion],
        selected_option_ids: set[UUID],
    ) -> tuple[set[UUID], set[UUID]]:
        """Compute which questions are applicable based on conditional dependencies.

        A question is applicable if:
        1. It has no depends_on_option, OR its depends_on_option was selected
        2. Its section (if any) is also applicable

        A section is applicable if:
        1. It has no depends_on_option, OR its depends_on_option was selected

        Args:
            mc_questions: Dictionary of all multiple choice questions by ID.
            ft_questions: Dictionary of all free text questions by ID.
            selected_option_ids: Set of option IDs that were selected in the submission.

        Returns:
            Tuple of (applicable_mcq_ids, applicable_ftq_ids).
        """
        # Determine applicable sections
        applicable_section_ids: set[UUID] = set()
        for section in self.questionnaire.sections.all():
            if section.depends_on_option_id is None or section.depends_on_option_id in selected_option_ids:
                applicable_section_ids.add(section.id)

        def is_question_applicable(question: MultipleChoiceQuestion | FreeTextQuestion) -> bool:
            # Check section applicability
            if question.section_id is not None and question.section_id not in applicable_section_ids:
                return False
            # Check direct option dependency
            if question.depends_on_option_id is not None and question.depends_on_option_id not in selected_option_ids:
                return False
            return True

        applicable_mcq_ids = {qid for qid, q in mc_questions.items() if is_question_applicable(q)}
        applicable_ftq_ids = {qid for qid, q in ft_questions.items() if is_question_applicable(q)}

        return applicable_mcq_ids, applicable_ftq_ids

    @transaction.atomic
    def submit(
        self,
        user: RevelUser,
        submission_schema: QuestionnaireSubmissionSchema,
        source_event: SubmissionSourceEventMetadata | None = None,
    ) -> QuestionnaireSubmission:
        """Perform a questionnaire submission.

        Args:
            user: The user submitting the questionnaire.
            submission_schema: The submission data.
            source_event: Optional event context metadata (stored in submission.metadata).
        """
        # Fetch and index all questions from the questionnaire
        mc_questions: dict[UUID, MultipleChoiceQuestion] = {
            q.id: q
            for q in list(self.questionnaire.multiplechoicequestion_questions.all())
            + [
                q
                for section in self.questionnaire.sections.all()
                for q in section.multiplechoicequestion_questions.all()
            ]
        }
        ft_questions: dict[UUID, FreeTextQuestion] = {
            q.id: q
            for q in list(self.questionnaire.freetextquestion_questions.all())
            + [q for section in self.questionnaire.sections.all() for q in section.freetextquestion_questions.all()]
        }

        all_question_ids = set(mc_questions.keys()) | set(ft_questions.keys())

        # Validate all answers point to the correct questionnaire
        submitted_question_ids = {
            *[a.question_id for a in submission_schema.multiple_choice_answers],
            *[a.question_id for a in submission_schema.free_text_answers],
        }

        if not submitted_question_ids.issubset(all_question_ids):
            raise CrossQuestionnaireSubmissionError("Some answers do not belong to this questionnaire.")

        # Extract selected option IDs from the submitted answers to determine applicable questions
        selected_option_ids: set[UUID] = set()
        for mc_answer in submission_schema.multiple_choice_answers:
            selected_option_ids.update(mc_answer.options_id)

        # Compute which questions are applicable based on conditional dependencies
        applicable_mcq_ids, applicable_ftq_ids = self._get_applicable_question_ids(
            mc_questions, ft_questions, selected_option_ids
        )

        # Validate that all applicable mandatory questions are answered
        mandatory_ids = {qid for qid, q in mc_questions.items() if q.is_mandatory and qid in applicable_mcq_ids} | {
            qid for qid, q in ft_questions.items() if q.is_mandatory and qid in applicable_ftq_ids
        }

        status = submission_schema.status

        if (
            not mandatory_ids.issubset(submitted_question_ids)
            and status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
        ):
            missing = mandatory_ids - submitted_question_ids
            raise MissingMandatoryAnswerError(
                f"Mandatory questions missing from submission: {[str(qid) for qid in missing]}"
            )

        # Build metadata dict if source_event is provided
        metadata: dict[str, t.Any] | None = None
        if source_event:
            metadata = {"source_event": source_event}

        # Create the submission and related answers
        if status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY:
            submission = QuestionnaireSubmission.objects.create(
                questionnaire=self.questionnaire,
                user=user,
                status=status,
                submitted_at=timezone.now(),
                metadata=metadata,
            )

            # Notifications are now handled by post_save signal in notifications/signals/questionnaire.py
        else:
            submission, _ = QuestionnaireSubmission.objects.update_or_create(
                questionnaire=self.questionnaire,
                user=user,
                status=submission_schema.status,
                defaults={"submitted_at": timezone.now(), "metadata": metadata},
            )
            submission.multiplechoiceanswer_answers.all().delete()
            submission.freetextanswer_answers.all().delete()

        mc_answers = [
            MultipleChoiceAnswer(
                submission=submission,
                question=mc_questions[answer.question_id],
                option_id=option_id,
            )
            for answer in submission_schema.multiple_choice_answers
            for option_id in answer.options_id
        ]
        MultipleChoiceAnswer.objects.bulk_create(mc_answers)

        ft_answers = [
            FreeTextAnswer(
                submission=submission,
                question=ft_questions[answer.question_id],
                answer=answer.answer,
            )
            for answer in submission_schema.free_text_answers
        ]
        FreeTextAnswer.objects.bulk_create(ft_answers)

        return submission

    @classmethod
    @transaction.atomic
    def create_questionnaire(cls, payload: QuestionnaireCreateSchema) -> Questionnaire:
        """Creates a complete Questionnaire, its sections, questions, and options from a Pydantic schema."""
        questionnaire_data = payload.model_dump(
            exclude={
                "sections",
                "multiplechoicequestion_questions",
                "freetextquestion_questions",
                "max_submission_age",
                "questionnaire_type",
                "members_exempt",
                "can_retake_after",  # Exclude because serializer converts timedelta to int
            }
        )
        # Add can_retake_after directly from schema (as timedelta, not serialized int)
        questionnaire_data["can_retake_after"] = payload.can_retake_after
        questionnaire = Questionnaire.objects.create(**questionnaire_data)
        service = cls(questionnaire.id)

        for mc_payload in payload.multiplechoicequestion_questions:
            service.create_mc_question(mc_payload)

        for ft_payload in payload.freetextquestion_questions:
            service.create_ft_question(ft_payload)

        for section_payload in payload.sections:
            service.create_section(section_payload)

        return questionnaire

    @transaction.atomic
    def create_section(
        self,
        payload: SectionCreateSchema,
        depends_on_option: MultipleChoiceOption | None = None,
    ) -> QuestionnaireSection:
        """Create a new section for the questionnaire.

        Args:
            payload: The section data.
            depends_on_option: Optional option that this section depends on (for conditional sections).
        """
        section_data = payload.model_dump(
            exclude={"multiplechoicequestion_questions", "freetextquestion_questions", "depends_on_option_id"}
        )
        section = QuestionnaireSection.objects.create(
            questionnaire=self.questionnaire,
            depends_on_option=depends_on_option,
            **section_data,
        )

        for mc_payload in payload.multiplechoicequestion_questions:
            self.create_mc_question(mc_payload, section)

        for ft_payload in payload.freetextquestion_questions:
            self.create_ft_question(ft_payload, section)

        return section

    @transaction.atomic
    def update_section(self, section: QuestionnaireSection, payload: SectionUpdateSchema) -> QuestionnaireSection:
        """Update a section of the questionnaire."""
        section.name = payload.name
        section.order = payload.order
        section.save()

        if payload.multiplechoicequestion_questions:
            section.multiplechoicequestion_questions.all().delete()
            for mc_payload in payload.multiplechoicequestion_questions:
                self.create_mc_question(mc_payload, section)

        if payload.freetextquestion_questions:
            section.freetextquestion_questions.all().delete()
            for ft_payload in payload.freetextquestion_questions:
                self.create_ft_question(ft_payload, section)

        return section

    @transaction.atomic
    def create_mc_question(
        self,
        payload: MultipleChoiceQuestionCreateSchema,
        section: QuestionnaireSection | None = None,
        depends_on_option: MultipleChoiceOption | None = None,
    ) -> MultipleChoiceQuestion:
        """Create a new multiple choice question for the questionnaire.

        Args:
            payload: The question data.
            section: Optional section to place the question in.
            depends_on_option: Optional option that this question depends on (for conditional questions).
        """
        if payload.section_id and section and payload.section_id != section.id:
            raise SectionIntegrityError("Section ID in payload does not match the provided section.")

        if payload.section_id:
            try:
                section = QuestionnaireSection.objects.get(id=payload.section_id, questionnaire=self.questionnaire)
            except QuestionnaireSection.DoesNotExist:
                raise SectionIntegrityError("Section does not exist or does not belong to this questionnaire.")

        options_data = payload.options
        mc_question_data = payload.model_dump(exclude={"options", "section_id", "depends_on_option_id"})
        mc_question = MultipleChoiceQuestion.objects.create(
            questionnaire=self.questionnaire,
            section=section,
            depends_on_option=depends_on_option,
            **mc_question_data,
        )

        # Create options and their nested conditional questions/sections
        for opt_payload in options_data:
            option = MultipleChoiceOption.objects.create(
                question=mc_question,
                option=opt_payload.option,
                is_correct=opt_payload.is_correct,
                order=opt_payload.order,
            )
            # Create nested conditional questions
            for cond_mc_payload in opt_payload.conditional_mc_questions:
                self.create_mc_question(cond_mc_payload, section=None, depends_on_option=option)
            for cond_ft_payload in opt_payload.conditional_ft_questions:
                self.create_ft_question(cond_ft_payload, section=None, depends_on_option=option)
            # Create nested conditional sections
            for cond_section_payload in opt_payload.conditional_sections:
                self.create_section(cond_section_payload, depends_on_option=option)

        return mc_question

    @transaction.atomic
    def update_mc_question(
        self, mc_question: MultipleChoiceQuestion, payload: MultipleChoiceQuestionUpdateSchema
    ) -> MultipleChoiceQuestion:
        """Update a multiple choice question of the questionnaire.

        Note: Updating options via this method does not support nested conditional creation.
        Use the flat depends_on_option_id approach for conditional questions when updating.
        """
        assert mc_question.questionnaire_id == self.questionnaire.id

        if payload.section_id and mc_question.section_id != payload.section_id:
            try:
                section = QuestionnaireSection.objects.get(id=payload.section_id, questionnaire=self.questionnaire)
                mc_question.section = section
            except QuestionnaireSection.DoesNotExist:
                raise SectionIntegrityError("Section does not exist or does not belong to this questionnaire.")

        for key, value in payload.model_dump(exclude={"options"}).items():
            setattr(mc_question, key, value)
        mc_question.save()

        if payload.options:
            mc_question.options.all().delete()
            options_to_create = [
                MultipleChoiceOption(
                    question=mc_question,
                    option=opt.option,
                    is_correct=opt.is_correct,
                    order=opt.order,
                )
                for opt in payload.options
            ]
            MultipleChoiceOption.objects.bulk_create(options_to_create)

        return mc_question

    def create_mc_option(
        self, question: MultipleChoiceQuestion, payload: MultipleChoiceOptionCreateSchema
    ) -> MultipleChoiceOption:
        """Create a new multiple choice option for a question.

        Note: This creates just the option. To create an option with nested conditionals,
        use the nested structure in create_mc_question's payload.
        """
        if question.questionnaire_id != self.questionnaire.id:
            raise QuestionIntegrityError("Question does not belong to this questionnaire.")
        option = MultipleChoiceOption.objects.create(
            question=question,
            option=payload.option,
            is_correct=payload.is_correct,
            order=payload.order,
        )
        # Create nested conditional questions/sections if any
        for cond_mc_payload in payload.conditional_mc_questions:
            self.create_mc_question(cond_mc_payload, section=None, depends_on_option=option)
        for cond_ft_payload in payload.conditional_ft_questions:
            self.create_ft_question(cond_ft_payload, section=None, depends_on_option=option)
        for cond_section_payload in payload.conditional_sections:
            self.create_section(cond_section_payload, depends_on_option=option)
        return option

    def update_mc_option(
        self, option: MultipleChoiceOption, payload: MultipleChoiceOptionUpdateSchema
    ) -> MultipleChoiceOption:
        """Update a multiple choice option of a question."""
        for key, value in payload.model_dump().items():
            setattr(option, key, value)
        option.save()
        return option

    @transaction.atomic
    def create_ft_question(
        self,
        payload: FreeTextQuestionCreateSchema,
        section: QuestionnaireSection | None = None,
        depends_on_option: MultipleChoiceOption | None = None,
    ) -> FreeTextQuestion:
        """Create a new free text question for the questionnaire.

        Args:
            payload: The question data.
            section: Optional section to place the question in.
            depends_on_option: Optional option that this question depends on (for conditional questions).
        """
        if payload.section_id and section and payload.section_id != section.id:
            raise SectionIntegrityError("Section ID in payload does not match the provided section.")

        if payload.section_id:
            try:
                section = QuestionnaireSection.objects.get(id=payload.section_id, questionnaire=self.questionnaire)
            except QuestionnaireSection.DoesNotExist:
                raise SectionIntegrityError("Section does not exist or does not belong to this questionnaire.")

        return FreeTextQuestion.objects.create(
            questionnaire=self.questionnaire,
            section=section,
            depends_on_option=depends_on_option,
            **payload.model_dump(exclude={"section_id", "depends_on_option_id"}),
        )

    @transaction.atomic
    def update_ft_question(
        self, ft_question: FreeTextQuestion, payload: FreeTextQuestionUpdateSchema
    ) -> FreeTextQuestion:
        """Update a free text question of the questionnaire."""
        assert ft_question.questionnaire_id == self.questionnaire.id

        if payload.section_id and ft_question.section_id != payload.section_id:
            try:
                section = QuestionnaireSection.objects.get(id=payload.section_id, questionnaire=self.questionnaire)
                ft_question.section = section
            except QuestionnaireSection.DoesNotExist:
                raise SectionIntegrityError("Section does not exist or does not belong to this questionnaire.")

        for key, value in payload.model_dump().items():
            setattr(ft_question, key, value)
        ft_question.save()
        return ft_question

    def get_submissions_queryset(self) -> QuerySet[QuestionnaireSubmission]:
        """Get submissions queryset for this questionnaire."""
        return (
            QuestionnaireSubmission.objects.filter(questionnaire=self.questionnaire)
            .select_related("user", "questionnaire")
            .prefetch_related("evaluation")
        )

    def get_submission_detail(self, submission_id: UUID) -> QuestionnaireSubmission:
        """Get detailed submission with answers."""
        return (
            QuestionnaireSubmission.objects.select_related("user", "questionnaire")
            .prefetch_related(
                "evaluation",
                "multiplechoiceanswer_answers__question",
                "multiplechoiceanswer_answers__option",
                "freetextanswer_answers__question",
            )
            .get(id=submission_id, questionnaire=self.questionnaire)
        )

    @transaction.atomic
    def evaluate_submission(
        self, submission_id: UUID, payload: EvaluationCreateSchema, evaluator: RevelUser
    ) -> QuestionnaireEvaluation:
        """Create or update an evaluation for a submission."""
        submission = QuestionnaireSubmission.objects.get(id=submission_id, questionnaire=self.questionnaire)

        evaluation, created = QuestionnaireEvaluation.objects.update_or_create(
            submission=submission,
            defaults={
                "status": payload.status,
                "score": payload.score,
                "comments": payload.comments,
                "evaluator": evaluator,
                "automatically_evaluated": False,
            },
        )

        # Notifications are now handled by post_save signal in notifications/signals/questionnaire.py

        return evaluation


def get_questionnaire_schema(questionnaire: Questionnaire) -> QuestionnaireCreateSchema:
    """Get the questionnaire schema."""
    return QuestionnaireCreateSchema.from_orm(questionnaire)
