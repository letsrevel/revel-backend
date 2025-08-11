from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field

# ---- Pydantic Models for Data Structures ----


class AnswerToEvaluate(BaseModel):
    """Represents a single free-text question and its answer to be sent for evaluation."""

    question_id: UUID = Field(..., description="The unique ID of the question being answered.")
    question_text: str
    answer_text: str
    guidelines: str


class EvaluationResult(BaseModel):
    """Represents the evaluation result for a single question from the LLM."""

    question_id: UUID = Field(..., description="The unique ID of the question that was evaluated.")
    is_passing: bool
    explanation: str


class EvaluationResponse(BaseModel):
    """The expected response from the LLM backend for a batch of questions."""

    evaluations: list[EvaluationResult]


# ---- The Protocol ----


class FreeTextEvaluator(Protocol):
    """Defines the interface for any class that can evaluate a BATCH of free-text answers."""

    def evaluate(
        self,
        *,
        questions_to_evaluate: list[AnswerToEvaluate],
        questionnaire_guidelines: str | None,
    ) -> EvaluationResponse:
        """Evaluates a batch of free-text answers against provided guidelines.

        Returns:
            A BatchEvaluationResponse object containing a list of individual
            evaluation results. The implementation MUST guarantee that every
            question sent is present in the response.
        """
