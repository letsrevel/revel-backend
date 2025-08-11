"""Custom exceptions for the questionnaires app."""

from django.core.exceptions import ValidationError


class QuestionnaireException(Exception):
    """Base exception for the questionnaires app."""


class CrossQuestionnaireSectionError(ValidationError):
    """Raised when a section is assigned to a question of a different questionnaire."""


class MultipleCorrectOptionsError(ValidationError):
    """Raised when multiple correct options are assigned to a single-answer question."""


class DisallowedMultipleAnswersError(ValidationError):
    """Raised when multiple answers are submitted for a single-answer question."""


class CrossQuestionnaireSubmissionError(QuestionnaireException):
    """Raised when a submission contains answers for a different questionnaire."""


class MissingMandatoryAnswerError(QuestionnaireException):
    """Raised when a mandatory answer is missing from a submission."""


class SectionIntegrityError(QuestionnaireException):
    """Raised when a section's integrity is compromised."""


class QuestionIntegrityError(QuestionnaireException):
    """Raised when a question's integrity is compromised."""


class SubmissionInDraftError(QuestionnaireException):
    """Raised when a submission is in draft mode and an evaluation is triggered."""


class SubmissionDoesNotExistError(QuestionnaireException):
    """Raised when a submission does not exist."""
