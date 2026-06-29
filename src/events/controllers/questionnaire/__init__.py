"""Questionnaire admin endpoints.

The 33 questionnaire routes are organised into focused mixins — core,
sections, questions, submissions, assignments — that are composed into a single
``QuestionnaireController``. Keeping one registered controller (and therefore
one class name) preserves the OpenAPI ``operationId`` stems, tags and route
ordering exactly, so the auto-generated frontend client is unaffected.
"""

from ninja_extra import api_controller

from common.authentication import I18nJWTAuth
from common.throttling import WriteThrottle

from .assignments import QuestionnaireAssignmentsMixin
from .core import QuestionnaireCoreMixin
from .questions import QuestionnaireQuestionsMixin
from .sections import QuestionnaireSectionsMixin
from .submissions import QuestionnaireSubmissionsMixin


@api_controller("/questionnaires", auth=I18nJWTAuth(), tags=["Questionnaires"], throttle=WriteThrottle())
class QuestionnaireController(
    QuestionnaireCoreMixin,
    QuestionnaireSectionsMixin,
    QuestionnaireQuestionsMixin,
    QuestionnaireSubmissionsMixin,
    QuestionnaireAssignmentsMixin,
):
    """Manage organization questionnaires, their structure, submissions and assignments."""


__all__ = ["QuestionnaireController"]
