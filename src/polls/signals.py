"""Block question/option/section mutations when the questionnaire belongs to a non-DRAFT poll."""

import typing as t
import uuid

from django.db import transaction
from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

from polls.exceptions import PollQuestionLockedError
from questionnaires.models import (
    FileUploadQuestion,
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    QuestionnaireSection,
)


def _questionnaire_id_from_instance(instance: t.Any) -> uuid.UUID | None:
    # All guarded models expose either ``.questionnaire_id`` (Question / Section)
    # or reach the questionnaire through ``.question.questionnaire_id`` (Option).
    if hasattr(instance, "questionnaire_id"):
        return t.cast(uuid.UUID | None, instance.questionnaire_id)
    if hasattr(instance, "question"):
        question = instance.question
        return t.cast(uuid.UUID | None, getattr(question, "questionnaire_id", None))
    return None


def _guard(instance: t.Any) -> None:
    """Reject question/option/section mutations on a non-DRAFT poll.

    The previous implementation used a plain ``filter().first()`` to read
    ``Poll.status``. That left a check-then-act race window where a concurrent
    transaction could transition the poll from DRAFT to OPEN (or DRAFT to any
    other status) AFTER the read and BEFORE the question-write committed —
    silently allowing a question edit on a poll that should have been locked.

    Wrap the lookup in an explicit ``transaction.atomic`` and acquire
    ``SELECT FOR UPDATE`` on the poll row so concurrent lifecycle transitions
    block until the question write commits or rolls back. Django tolerates
    nested ``atomic()`` calls (savepoints), so this is safe even when the
    caller already opened its own transaction.
    """
    from polls.models import Poll

    questionnaire_id = _questionnaire_id_from_instance(instance)
    if questionnaire_id is None:
        return
    with transaction.atomic():
        poll = Poll.objects.select_for_update().filter(questionnaire_id=questionnaire_id).only("status").first()
        if poll is None:
            return
        if poll.status != Poll.PollStatus.DRAFT:
            raise PollQuestionLockedError()


@receiver(pre_save, sender=MultipleChoiceQuestion)
@receiver(pre_save, sender=FreeTextQuestion)
@receiver(pre_save, sender=FileUploadQuestion)
@receiver(pre_save, sender=MultipleChoiceOption)
@receiver(pre_save, sender=QuestionnaireSection)
def _block_save(sender: t.Any, instance: t.Any, **kwargs: t.Any) -> None:
    _guard(instance)


@receiver(pre_delete, sender=MultipleChoiceQuestion)
@receiver(pre_delete, sender=FreeTextQuestion)
@receiver(pre_delete, sender=FileUploadQuestion)
@receiver(pre_delete, sender=MultipleChoiceOption)
@receiver(pre_delete, sender=QuestionnaireSection)
def _block_delete(sender: t.Any, instance: t.Any, **kwargs: t.Any) -> None:
    _guard(instance)
