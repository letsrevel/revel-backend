import random
import typing as t
from uuid import UUID

from django.db import transaction
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
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
    QuestionnaireEvaluation,
    QuestionnaireFile,
    QuestionnaireSubmission,
    SubmissionSourceEventMetadata,
)

from ..exceptions import (
    CrossQuestionnaireSubmissionError,
    DisallowedMultipleAnswersError,
    FileLimitExceededError,
    FileOwnershipError,
    FileSizeExceededError,
    InvalidFileMimeTypeError,
    MissingMandatoryAnswerError,
)
from ..schema import (
    EvaluationCreateSchema,
    FileUploadQuestionSchema,
    FileUploadSubmissionSchema,
    FreeTextQuestionSchema,
    MultipleChoiceOptionSchema,
    MultipleChoiceQuestionSchema,
    QuestionnaireSchema,
    QuestionnaireSubmissionSchema,
    SectionSchema,
)
from .questionnaire_service import load_questionnaire_with_relations


class SubmissionService:
    def __init__(self, questionnaire_id: UUID) -> None:
        """Initialize submission service."""
        self.questionnaire = load_questionnaire_with_relations(questionnaire_id)

    def build(self, shuffle_seed: int | str | bytes | None = None) -> QuestionnaireSchema:
        """Build questionnaire schema.

        Args:
            shuffle_seed: Optional deterministic seed for question/option/section shuffling.
                When provided, the shuffle order is stable for the same seed (e.g. a per-user,
                per-questionnaire seed) instead of re-randomizing on every page load (see #509).
                When ``None`` the order is randomized on each call, preserving the legacy behaviour.
        """
        q = self.questionnaire
        rng = random.Random(shuffle_seed)

        def build_mc_question(mcq: MultipleChoiceQuestion) -> MultipleChoiceQuestionSchema:
            options = list(mcq.options.all())
            if mcq.shuffle_options:
                rng.shuffle(options)
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
                rng.shuffle(combined)
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
            rng.shuffle(sections)
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

    def _collect_all_questions(
        self,
    ) -> tuple[
        dict[UUID, MultipleChoiceQuestion],
        dict[UUID, FreeTextQuestion],
        dict[UUID, FileUploadQuestion],
    ]:
        """Fetch and index all questions from the questionnaire and its sections."""
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
        return mc_questions, ft_questions, fu_questions

    def _validate_submission(
        self,
        submission_schema: QuestionnaireSubmissionSchema,
        mc_questions: dict[UUID, MultipleChoiceQuestion],
        ft_questions: dict[UUID, FreeTextQuestion],
        fu_questions: dict[UUID, FileUploadQuestion],
    ) -> None:
        """Validate that answers belong to this questionnaire and mandatory questions are answered.

        Raises:
            CrossQuestionnaireSubmissionError: If answers reference questions from another questionnaire.
            MissingMandatoryAnswerError: If mandatory applicable questions are not answered (READY only).
        """
        all_question_ids = set(mc_questions.keys()) | set(ft_questions.keys()) | set(fu_questions.keys())

        submitted_question_ids = {
            *[a.question_id for a in submission_schema.multiple_choice_answers],
            *[a.question_id for a in submission_schema.free_text_answers],
            *[a.question_id for a in submission_schema.file_upload_answers],
        }

        if not submitted_question_ids.issubset(all_question_ids):
            raise CrossQuestionnaireSubmissionError("Some answers do not belong to this questionnaire.")

        # Each selected option must belong to the multiple-choice question it is paired with.
        # Without this, a submitter could pair a legitimate question with an arbitrary option id
        # from another question/questionnaire — the answer is bulk-created (clean() skipped) and
        # the evaluator credits the paired question's weight for that foreign option's is_correct
        # flag, inflating the score and (in AUTOMATIC mode) auto-approving an admission gate.
        # Query (question, option) pairs fresh rather than reading question.options.all(): the
        # questionnaire is prefetched at service construction, so its options cache can be stale.
        allowed_option_pairs = set(
            MultipleChoiceOption.objects.filter(question_id__in=mc_questions.keys()).values_list("question_id", "id")
        )
        for mc_answer in submission_schema.multiple_choice_answers:
            for option_id in mc_answer.options_id:
                if (mc_answer.question_id, option_id) not in allowed_option_pairs:
                    raise CrossQuestionnaireSubmissionError("Some selected options do not belong to their question.")

        # Enforce the single-answer invariant on the write path. ``MultipleChoiceAnswer.clean()``
        # is the only other guard, but answers are bulk-created (clean() skipped), so without this
        # a submitter could pair a single-answer question with *every* option — guaranteeing the
        # correct one is selected and (in AUTOMATIC mode) auto-approving an admission gate.
        # Aggregate options per question across ALL answer entries (de-duplicated): the schema
        # accepts a list of answers, so a client could otherwise split selections into several
        # entries for the same single-answer question (one option each), slipping past a
        # per-entry length check while ``_create_answers`` still persists one row per option.
        options_by_question: dict[UUID, set[UUID]] = {}
        for mc_answer in submission_schema.multiple_choice_answers:
            options_by_question.setdefault(mc_answer.question_id, set()).update(mc_answer.options_id)
        for question_id, option_ids in options_by_question.items():
            question = mc_questions.get(question_id)
            if question is not None and question.allow_multiple_answers is False and len(option_ids) > 1:
                raise DisallowedMultipleAnswersError(
                    {"question": "Multiple answers are not allowed for this question."}
                )

        selected_option_ids: set[UUID] = set()
        for mc_answer in submission_schema.multiple_choice_answers:
            selected_option_ids.update(mc_answer.options_id)

        applicable_mcq_ids, applicable_ftq_ids, applicable_fuq_ids = self._get_applicable_question_ids(
            mc_questions, ft_questions, fu_questions, selected_option_ids
        )

        mandatory_ids = (
            {qid for qid, q in mc_questions.items() if q.is_mandatory and qid in applicable_mcq_ids}
            | {qid for qid, q in ft_questions.items() if q.is_mandatory and qid in applicable_ftq_ids}
            | {qid for qid, q in fu_questions.items() if q.is_mandatory and qid in applicable_fuq_ids}
        )

        # A multiple-choice answer with no selected options does not *answer* its question:
        # ``_create_answers`` writes one row per option, so an empty ``options_id`` persists
        # nothing. Counting it would let a mandatory MC question be marked answered while READY
        # with no actual selection. Filter empties out of the mandatory set only — optional
        # questions may still be submitted empty (and DRAFT saves are unaffected, see status
        # gate below). Free-text (``min_length=1``) and file-upload (``min_length=1``) answers
        # are non-empty by schema, so only MC needs filtering.
        answered_question_ids = {
            *[a.question_id for a in submission_schema.multiple_choice_answers if a.options_id],
            *[a.question_id for a in submission_schema.free_text_answers],
            *[a.question_id for a in submission_schema.file_upload_answers],
        }

        if (
            not mandatory_ids.issubset(answered_question_ids)
            and submission_schema.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
        ):
            missing = mandatory_ids - answered_question_ids
            raise MissingMandatoryAnswerError(
                f"Mandatory questions missing from submission: {[str(qid) for qid in missing]}"
            )

    @staticmethod
    def _create_answers(
        submission: QuestionnaireSubmission,
        submission_schema: QuestionnaireSubmissionSchema,
        mc_questions: dict[UUID, MultipleChoiceQuestion],
        ft_questions: dict[UUID, FreeTextQuestion],
    ) -> None:
        """Bulk-create multiple-choice and free-text answers for a submission."""
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
        mc_questions, ft_questions, fu_questions = self._collect_all_questions()
        self._validate_submission(submission_schema, mc_questions, ft_questions, fu_questions)

        # Build metadata dict if source_event is provided
        metadata: dict[str, SubmissionSourceEventMetadata] | None = None
        if source_event:
            metadata = {"source_event": source_event}

        status = submission_schema.status

        # Create the submission record
        if status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY:
            submission = QuestionnaireSubmission.objects.create(
                questionnaire=self.questionnaire,
                user=user,
                status=status,
                submitted_at=timezone.now(),
                metadata=metadata,
            )
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

        self._create_answers(submission, submission_schema, mc_questions, ft_questions)
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
            # Build an expanded set that includes equivalent MIME types:
            # - audio/mp4 <-> video/mp4 (.m4a files detected differently by libmagic)
            # - audio/webm <-> video/webm (browser MediaRecorder containers)
            expanded = set(question.allowed_mime_types)
            if "audio/mp4" in expanded or "video/mp4" in expanded:
                expanded.update({"audio/mp4", "video/mp4"})
            if "audio/webm" in expanded or "video/webm" in expanded:
                expanded.update({"audio/webm", "video/webm"})
            for f in files:
                if f.mime_type not in expanded:
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

    def get_submissions_queryset(self) -> QuerySet[QuestionnaireSubmission]:
        """Get submissions queryset for this questionnaire."""
        return (
            QuestionnaireSubmission.objects.filter(questionnaire=self.questionnaire)
            .select_related("user", "questionnaire", "questionnaire__org_questionnaires")
            .prefetch_related("evaluation")
        )

    def get_submission_detail(self, submission_id: UUID) -> QuestionnaireSubmission:
        """Get detailed submission with answers."""
        qs = QuestionnaireSubmission.objects.select_related(
            "user", "questionnaire", "questionnaire__org_questionnaires"
        ).prefetch_related(
            "evaluation",
            "multiplechoiceanswer_answers__question",
            "multiplechoiceanswer_answers__option",
            "freetextanswer_answers__question",
            "fileuploadanswer_answers__question",
            "fileuploadanswer_answers__files",
        )
        return get_object_or_404(qs, id=submission_id, questionnaire=self.questionnaire)

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
