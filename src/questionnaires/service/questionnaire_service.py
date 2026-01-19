import random
import typing as t
from uuid import UUID

from django.db import transaction
from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from accounts.models import RevelUser
from questionnaires.models import (
    FileUploadAnswer,
    FileUploadQuestion,
    FreeTextAnswer,
    FreeTextQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireFile,
    QuestionnaireSection,
    QuestionnaireSubmission,
    SubmissionSourceEventMetadata,
)

from ..exceptions import (
    CrossQuestionnaireSubmissionError,
    FileLimitExceededError,
    FileOwnershipError,
    FileSizeExceededError,
    InvalidFileMimeTypeError,
    MissingMandatoryAnswerError,
    QuestionIntegrityError,
    SectionIntegrityError,
)
from ..schema import (
    EvaluationCreateSchema,
    FileUploadQuestionCreateSchema,
    FileUploadQuestionSchema,
    FileUploadQuestionUpdateSchema,
    FileUploadSubmissionSchema,
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
    QuestionnaireResponseSchema,
    QuestionnaireSchema,
    QuestionnaireSubmissionSchema,
    SectionCreateSchema,
    SectionSchema,
    SectionUpdateSchema,
)

QuestionSchemaUnion = MultipleChoiceQuestionSchema | FreeTextQuestionSchema | FileUploadQuestionSchema
QuestionCreatePayload = (
    MultipleChoiceQuestionCreateSchema | FreeTextQuestionCreateSchema | FileUploadQuestionCreateSchema
)


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
            Prefetch(
                "fileuploadquestion_questions",
                queryset=FileUploadQuestion.objects.all(),
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
            Prefetch(
                "fileuploadquestion_questions",
                queryset=FileUploadQuestion.objects.filter(section__isnull=True),
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

        def build_fu_question(fuq: FileUploadQuestion) -> FileUploadQuestionSchema:
            return FileUploadQuestionSchema(
                id=fuq.id,
                question=fuq.question or "",
                hint=fuq.hint,
                is_mandatory=fuq.is_mandatory,
                order=fuq.order,
                allowed_mime_types=fuq.allowed_mime_types,
                max_file_size=fuq.max_file_size,
                max_files=fuq.max_files,
                depends_on_option_id=fuq.depends_on_option_id,
            )

        def build_question_block(
            _mcqs: list[MultipleChoiceQuestion],
            _ftqs: list[FreeTextQuestion],
            _fuqs: list[FileUploadQuestion],
        ) -> tuple[
            list[MultipleChoiceQuestionSchema],
            list[FreeTextQuestionSchema],
            list[FileUploadQuestionSchema],
        ]:
            if q.shuffle_questions:
                combined: list[tuple[int, t.Any]] = (
                    [(0, mcq) for mcq in _mcqs] + [(1, ftq) for ftq in _ftqs] + [(2, fuq) for fuq in _fuqs]
                )
                random.shuffle(combined)
                _mcqs = [obj for kind, obj in combined if kind == 0]
                _ftqs = [obj for kind, obj in combined if kind == 1]
                _fuqs = [obj for kind, obj in combined if kind == 2]
            else:
                _mcqs.sort(key=lambda x: x.order)
                _ftqs.sort(key=lambda x: x.order)
                _fuqs.sort(key=lambda x: x.order)

            return (
                [build_mc_question(mcq) for mcq in _mcqs],
                [build_ft_question(ftq) for ftq in _ftqs],
                [build_fu_question(fuq) for fuq in _fuqs],
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
            fuqs = list(section.fileuploadquestion_questions.all())
            mcq_schemas, ftq_schemas, fuq_schemas = build_question_block(mcqs, ftqs, fuqs)

            section_schemas.append(
                SectionSchema(
                    id=section.id,
                    name=section.name,
                    description=section.description,
                    order=section.order,
                    multiple_choice_questions=mcq_schemas,
                    free_text_questions=ftq_schemas,
                    file_upload_questions=fuq_schemas,
                    depends_on_option_id=section.depends_on_option_id,
                )
            )

        # Build top-level (unsectioned) questions
        mcqs = list(q.multiplechoicequestion_questions.all())
        ftqs = list(q.freetextquestion_questions.all())
        fuqs = list(q.fileuploadquestion_questions.all())
        mcq_schemas, ftq_schemas, fuq_schemas = build_question_block(mcqs, ftqs, fuqs)

        return QuestionnaireSchema(
            id=q.id,
            name=q.name,
            description=q.description,
            multiple_choice_questions=mcq_schemas,
            free_text_questions=ftq_schemas,
            file_upload_questions=fuq_schemas,
            sections=section_schemas,
            evaluation_mode=q.evaluation_mode,  # type: ignore[arg-type]
        )

    def _get_applicable_question_ids(
        self,
        mc_questions: dict[UUID, MultipleChoiceQuestion],
        ft_questions: dict[UUID, FreeTextQuestion],
        fu_questions: dict[UUID, FileUploadQuestion],
        selected_option_ids: set[UUID],
    ) -> tuple[set[UUID], set[UUID], set[UUID]]:
        """Compute which questions are applicable based on conditional dependencies.

        A question is applicable if:
        1. It has no depends_on_option, OR its depends_on_option was selected
        2. Its section (if any) is also applicable

        A section is applicable if:
        1. It has no depends_on_option, OR its depends_on_option was selected

        Args:
            mc_questions: Dictionary of all multiple choice questions by ID.
            ft_questions: Dictionary of all free text questions by ID.
            fu_questions: Dictionary of all file upload questions by ID.
            selected_option_ids: Set of option IDs that were selected in the submission.

        Returns:
            Tuple of (applicable_mcq_ids, applicable_ftq_ids, applicable_fuq_ids).
        """
        # Determine applicable sections
        applicable_section_ids: set[UUID] = set()
        for section in self.questionnaire.sections.all():
            if section.depends_on_option_id is None or section.depends_on_option_id in selected_option_ids:
                applicable_section_ids.add(section.id)

        def is_question_applicable(
            question: MultipleChoiceQuestion | FreeTextQuestion | FileUploadQuestion,
        ) -> bool:
            # Check section applicability
            if question.section_id is not None and question.section_id not in applicable_section_ids:
                return False
            # Check direct option dependency
            if question.depends_on_option_id is not None and question.depends_on_option_id not in selected_option_ids:
                return False
            return True

        applicable_mcq_ids = {qid for qid, q in mc_questions.items() if is_question_applicable(q)}
        applicable_ftq_ids = {qid for qid, q in ft_questions.items() if is_question_applicable(q)}
        applicable_fuq_ids = {qid for qid, q in fu_questions.items() if is_question_applicable(q)}

        return applicable_mcq_ids, applicable_ftq_ids, applicable_fuq_ids

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
        fu_questions: dict[UUID, FileUploadQuestion] = {
            q.id: q
            for q in list(self.questionnaire.fileuploadquestion_questions.all())
            + [q for section in self.questionnaire.sections.all() for q in section.fileuploadquestion_questions.all()]
        }

        all_question_ids = set(mc_questions.keys()) | set(ft_questions.keys()) | set(fu_questions.keys())

        # Validate all answers point to the correct questionnaire
        submitted_question_ids = {
            *[a.question_id for a in submission_schema.multiple_choice_answers],
            *[a.question_id for a in submission_schema.free_text_answers],
            *[a.question_id for a in submission_schema.file_upload_answers],
        }

        if not submitted_question_ids.issubset(all_question_ids):
            raise CrossQuestionnaireSubmissionError("Some answers do not belong to this questionnaire.")

        # Extract selected option IDs from the submitted answers to determine applicable questions
        selected_option_ids: set[UUID] = set()
        for mc_answer in submission_schema.multiple_choice_answers:
            selected_option_ids.update(mc_answer.options_id)

        # Compute which questions are applicable based on conditional dependencies
        applicable_mcq_ids, applicable_ftq_ids, applicable_fuq_ids = self._get_applicable_question_ids(
            mc_questions, ft_questions, fu_questions, selected_option_ids
        )

        # Validate that all applicable mandatory questions are answered
        mandatory_ids = (
            {qid for qid, q in mc_questions.items() if q.is_mandatory and qid in applicable_mcq_ids}
            | {qid for qid, q in ft_questions.items() if q.is_mandatory and qid in applicable_ftq_ids}
            | {qid for qid, q in fu_questions.items() if q.is_mandatory and qid in applicable_fuq_ids}
        )

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
            submission.fileuploadanswer_answers.all().delete()

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

        # Create file upload answers with M2M relationships
        self._create_file_upload_answers(submission, submission_schema, fu_questions, user)

        return submission

    def _create_file_upload_answers(
        self,
        submission: QuestionnaireSubmission,
        submission_schema: QuestionnaireSubmissionSchema,
        fu_questions: dict[UUID, FileUploadQuestion],
        user: RevelUser,
    ) -> None:
        """Create file upload answers with validation.

        Validates that:
        - Files belong to the submitting user
        - File count doesn't exceed question.max_files
        - File MIME types match allowed types (if restricted)
        - File sizes don't exceed question.max_file_size
        """
        for fu_answer_schema in submission_schema.file_upload_answers:
            question = fu_questions[fu_answer_schema.question_id]
            files = self._validate_file_upload_answer(fu_answer_schema, question, user)

            fu_answer = FileUploadAnswer.objects.create(
                submission=submission,
                question=question,
            )
            fu_answer.files.set(files)

    def _validate_file_upload_answer(
        self,
        fu_answer_schema: FileUploadSubmissionSchema,
        question: FileUploadQuestion,
        user: RevelUser,
    ) -> QuerySet[QuestionnaireFile]:
        """Validate files for a file upload answer.

        Returns the validated queryset of files.

        Raises:
            FileOwnershipError: If some files don't exist or don't belong to the user.
            FileLimitExceededError: If file count exceeds question's max_files.
            InvalidFileMimeTypeError: If file MIME type doesn't match allowed types.
            FileSizeExceededError: If file size exceeds question's max_file_size.
        """
        files = QuestionnaireFile.objects.filter(
            id__in=fu_answer_schema.file_ids,
            uploader=user,
        )
        if files.count() != len(fu_answer_schema.file_ids):
            raise FileOwnershipError("Some files do not exist or do not belong to you.")
        if files.count() > question.max_files:
            raise FileLimitExceededError(f"Too many files. Maximum allowed: {question.max_files}.")
        # Validate MIME types if restricted
        if question.allowed_mime_types:
            for f in files:
                if f.mime_type not in question.allowed_mime_types:
                    raise InvalidFileMimeTypeError(
                        f"File '{f.original_filename}' has type '{f.mime_type}' which is not allowed. "
                        f"Allowed types: {', '.join(question.allowed_mime_types)}."
                    )
        # Validate file sizes
        for f in files:
            if f.file_size > question.max_file_size:
                raise FileSizeExceededError(
                    f"File '{f.original_filename}' ({f.file_size} bytes) exceeds maximum size "
                    f"of {question.max_file_size} bytes."
                )
        return files

    @classmethod
    @transaction.atomic
    def create_questionnaire(cls, payload: QuestionnaireCreateSchema) -> Questionnaire:
        """Creates a complete Questionnaire, its sections, questions, and options from a Pydantic schema."""
        questionnaire_data = payload.model_dump(
            exclude={
                "sections",
                "multiplechoicequestion_questions",
                "freetextquestion_questions",
                "fileuploadquestion_questions",
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

        for fu_payload in payload.fileuploadquestion_questions:
            service.create_fu_question(fu_payload)

        for section_payload in payload.sections:
            service.create_section(section_payload)

        return questionnaire

    def _resolve_question_dependencies(
        self,
        payload: QuestionCreatePayload,
        section: QuestionnaireSection | None = None,
        depends_on_option: MultipleChoiceOption | None = None,
    ) -> tuple[QuestionnaireSection | None, MultipleChoiceOption | None]:
        """Resolve and validate section and option dependencies for questions.

        Args:
            payload: The question creation payload containing section_id and depends_on_option_id.
            section: Optional section already resolved (e.g., when creating nested questions).
            depends_on_option: Optional option already resolved (e.g., for conditional questions).

        Returns:
            Tuple of (resolved_section, resolved_depends_on_option).

        Raises:
            SectionIntegrityError: If section_id doesn't match provided section or doesn't exist.
            QuestionIntegrityError: If depends_on_option_id doesn't exist in this questionnaire.
        """
        # Validate section consistency if both provided
        if payload.section_id and section and payload.section_id != section.id:
            raise SectionIntegrityError("Section ID in payload does not match the provided section.")

        # Resolve section from payload if not provided as parameter
        if payload.section_id and section is None:
            try:
                section = QuestionnaireSection.objects.get(id=payload.section_id, questionnaire=self.questionnaire)
            except QuestionnaireSection.DoesNotExist:
                raise SectionIntegrityError("Section does not exist or does not belong to this questionnaire.")

        # Resolve depends_on_option from payload if not provided as parameter
        if depends_on_option is None and payload.depends_on_option_id:
            try:
                depends_on_option = MultipleChoiceOption.objects.get(
                    id=payload.depends_on_option_id,
                    question__questionnaire=self.questionnaire,
                )
            except MultipleChoiceOption.DoesNotExist:
                raise QuestionIntegrityError("Option does not exist or does not belong to this questionnaire.")

        return section, depends_on_option

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
                If not provided but payload.depends_on_option_id is set, it will be resolved.
        """
        # Resolve depends_on_option from payload if not provided as parameter
        if depends_on_option is None and payload.depends_on_option_id:
            try:
                depends_on_option = MultipleChoiceOption.objects.get(
                    id=payload.depends_on_option_id,
                    question__questionnaire=self.questionnaire,
                )
            except MultipleChoiceOption.DoesNotExist:
                raise SectionIntegrityError("Option does not exist or does not belong to this questionnaire.")

        section_data = payload.model_dump(
            exclude={
                "multiplechoicequestion_questions",
                "freetextquestion_questions",
                "fileuploadquestion_questions",
                "depends_on_option_id",
            }
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

        for fu_payload in payload.fileuploadquestion_questions:
            self.create_fu_question(fu_payload, section)

        return section

    def update_section(self, section: QuestionnaireSection, payload: SectionUpdateSchema) -> QuestionnaireSection:
        """Update a section of the questionnaire.

        Only updates section metadata (name, description, order, depends_on_option).
        Questions must be added/updated/deleted via dedicated endpoints.
        """
        section.name = payload.name
        section.description = payload.description
        section.order = payload.order
        section.depends_on_option_id = payload.depends_on_option_id
        section.save(update_fields=["name", "description", "order", "depends_on_option_id", "updated_at"])
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
                If not provided but payload.depends_on_option_id is set, it will be resolved.
        """
        section, depends_on_option = self._resolve_question_dependencies(payload, section, depends_on_option)

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
            self._create_conditional_content_for_option(opt_payload, option)

        return mc_question

    def _create_conditional_content_for_option(
        self,
        opt_payload: MultipleChoiceOptionCreateSchema,
        option: MultipleChoiceOption,
    ) -> None:
        """Create nested conditional questions and sections for an option."""
        for cond_mc_payload in opt_payload.conditional_mc_questions:
            self.create_mc_question(cond_mc_payload, section=None, depends_on_option=option)
        for cond_ft_payload in opt_payload.conditional_ft_questions:
            self.create_ft_question(cond_ft_payload, section=None, depends_on_option=option)
        for cond_fu_payload in opt_payload.conditional_fu_questions:
            self.create_fu_question(cond_fu_payload, section=None, depends_on_option=option)
        for cond_section_payload in opt_payload.conditional_sections:
            self.create_section(cond_section_payload, depends_on_option=option)

    def update_mc_question(
        self, mc_question: MultipleChoiceQuestion, payload: MultipleChoiceQuestionUpdateSchema
    ) -> MultipleChoiceQuestion:
        """Update a multiple choice question of the questionnaire.

        Only updates question metadata. Options must be added/updated/deleted
        via dedicated endpoints to prevent accidental data loss.
        """
        assert mc_question.questionnaire_id == self.questionnaire.id

        if payload.section_id and mc_question.section_id != payload.section_id:
            try:
                section = QuestionnaireSection.objects.get(id=payload.section_id, questionnaire=self.questionnaire)
                mc_question.section = section
            except QuestionnaireSection.DoesNotExist:
                raise SectionIntegrityError("Section does not exist or does not belong to this questionnaire.")

        for key, value in payload.model_dump(exclude={"section_id"}).items():
            setattr(mc_question, key, value)
        mc_question.save()

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
        self._create_conditional_content_for_option(payload, option)
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
                If not provided but payload.depends_on_option_id is set, it will be resolved.
        """
        section, depends_on_option = self._resolve_question_dependencies(payload, section, depends_on_option)

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

    @transaction.atomic
    def create_fu_question(
        self,
        payload: FileUploadQuestionCreateSchema,
        section: QuestionnaireSection | None = None,
        depends_on_option: MultipleChoiceOption | None = None,
    ) -> FileUploadQuestion:
        """Create a new file upload question for the questionnaire.

        Args:
            payload: The question data.
            section: Optional section to place the question in.
            depends_on_option: Optional option that this question depends on (for conditional questions).
                If not provided but payload.depends_on_option_id is set, it will be resolved.
        """
        section, depends_on_option = self._resolve_question_dependencies(payload, section, depends_on_option)

        return FileUploadQuestion.objects.create(
            questionnaire=self.questionnaire,
            section=section,
            depends_on_option=depends_on_option,
            **payload.model_dump(exclude={"section_id", "depends_on_option_id"}),
        )

    @transaction.atomic
    def update_fu_question(
        self, fu_question: FileUploadQuestion, payload: FileUploadQuestionUpdateSchema
    ) -> FileUploadQuestion:
        """Update a file upload question of the questionnaire."""
        assert fu_question.questionnaire_id == self.questionnaire.id

        if payload.section_id and fu_question.section_id != payload.section_id:
            try:
                section = QuestionnaireSection.objects.get(id=payload.section_id, questionnaire=self.questionnaire)
                fu_question.section = section
            except QuestionnaireSection.DoesNotExist:
                raise SectionIntegrityError("Section does not exist or does not belong to this questionnaire.")

        for key, value in payload.model_dump(exclude={"section_id"}).items():
            setattr(fu_question, key, value)
        fu_question.save()
        return fu_question

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
                "fileuploadanswer_answers__question",
                "fileuploadanswer_answers__files",
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


def get_questionnaire_schema(questionnaire: Questionnaire) -> QuestionnaireResponseSchema:
    """Get the questionnaire schema for API responses.

    Uses prefetch to ensure top-level questions arrays only contain questions
    without a section, avoiding duplication with section-level questions.
    Returns QuestionnaireResponseSchema which includes id fields for all nested objects.
    """
    # Prefetch with proper filtering to avoid duplicate questions
    questionnaire = Questionnaire.objects.prefetch_related(
        Prefetch(
            "multiplechoicequestion_questions",
            queryset=MultipleChoiceQuestion.objects.filter(section__isnull=True).prefetch_related("options"),
        ),
        Prefetch(
            "freetextquestion_questions",
            queryset=FreeTextQuestion.objects.filter(section__isnull=True),
        ),
        Prefetch(
            "fileuploadquestion_questions",
            queryset=FileUploadQuestion.objects.filter(section__isnull=True),
        ),
        Prefetch(
            "sections",
            queryset=QuestionnaireSection.objects.prefetch_related(
                Prefetch(
                    "multiplechoicequestion_questions",
                    queryset=MultipleChoiceQuestion.objects.prefetch_related("options"),
                ),
                "freetextquestion_questions",
                "fileuploadquestion_questions",
            ).order_by("order"),
        ),
    ).get(pk=questionnaire.pk)
    return QuestionnaireResponseSchema.from_orm(questionnaire)
