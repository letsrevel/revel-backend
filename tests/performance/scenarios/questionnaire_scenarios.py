# tests/performance/scenarios/questionnaire_scenarios.py
"""Questionnaire submission performance test scenarios.

Tests BOTTLENECK endpoint:
- /events/{event_id}/questionnaire/{qid}/submit
"""

from locust import task
from scenarios.base import AuthenticatedRevelUser


class QuestionnaireUser(AuthenticatedRevelUser):
    """Scenario: Questionnaire submission.

    Tests the questionnaire submission flow (BOTTLENECK):
    1. GET /events/{id}/my-status (to get questionnaire info)
    2. GET /events/{id}/questionnaire/{qid}
    3. POST /events/{id}/questionnaire/{qid}/submit
    4. GET /events/{id}/my-status (verify)

    Weight: 5 (bottleneck testing)

    Note: This scenario expects a simple questionnaire with one
    multiple choice question. The bootstrap_perf_tests command
    should create such a questionnaire.
    """

    abstract = False

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._event_id: str | None = None
        self._questionnaire_id: str | None = None
        self._question_id: str | None = None
        self._correct_option_id: str | None = None

    def on_start(self) -> None:
        """Login and cache questionnaire info."""
        super().on_start()
        self._event_id = self.get_perf_questionnaire_event_id()

        if self._event_id:
            # Get my-status to find questionnaire ID
            status = self.api.get_my_status(self._event_id)
            if status:
                # Look for questionnaire in next_step or eligibility info
                # The exact structure depends on your API response
                questionnaires = status.get("questionnaires", [])
                if questionnaires:
                    self._questionnaire_id = questionnaires[0].get("id")

                # If questionnaire ID found, get the questionnaire structure
                if self._questionnaire_id:
                    self._load_questionnaire_structure()

    def _load_questionnaire_structure(self) -> None:
        """Load questionnaire and extract question/option IDs."""
        if not self._event_id or not self._questionnaire_id:
            return

        questionnaire = self.api.get_questionnaire(self._event_id, self._questionnaire_id)
        if not questionnaire:
            return

        # Find first multiple choice question
        questions = questionnaire.get("questionnaire", {}).get("multiplechoicequestion_questions", [])
        if not questions:
            # Check sections
            sections = questionnaire.get("questionnaire", {}).get("sections", [])
            for section in sections:
                questions = section.get("multiplechoicequestion_questions", [])
                if questions:
                    break

        if questions:
            question = questions[0]
            self._question_id = question.get("id")

            # Find correct option (or first option)
            options = question.get("options", [])
            for option in options:
                if option.get("is_correct"):
                    self._correct_option_id = option.get("id")
                    break
            if not self._correct_option_id and options:
                self._correct_option_id = options[0].get("id")

    @task(2)
    def check_questionnaire_status(self) -> None:
        """Check status and questionnaire availability."""
        if not self._event_id:
            return
        self.api.get_my_status(self._event_id)

    @task(1)
    def submit_questionnaire(self) -> None:
        """Submit questionnaire answers (BOTTLENECK)."""
        if not self._event_id or not self._questionnaire_id:
            return
        if not self._question_id or not self._correct_option_id:
            return

        answers = {
            "status": "ready",
            "multiple_choice_answers": [
                {
                    "question_id": self._question_id,
                    "options": [self._correct_option_id],
                }
            ],
            "free_text_answers": [],
            "file_upload_answers": [],
        }

        self.api.submit_questionnaire(self._event_id, self._questionnaire_id, answers)

    @task(1)
    def full_questionnaire_flow(self) -> None:
        """Execute full questionnaire submission flow."""
        if not self._event_id or not self._questionnaire_id:
            return
        if not self._question_id or not self._correct_option_id:
            return

        # Check initial status
        self.api.get_my_status(self._event_id)

        # Get questionnaire (simulate reading questions)
        self.api.get_questionnaire(self._event_id, self._questionnaire_id)

        # Submit
        answers = {
            "status": "ready",
            "multiple_choice_answers": [
                {
                    "question_id": self._question_id,
                    "options": [self._correct_option_id],
                }
            ],
            "free_text_answers": [],
            "file_upload_answers": [],
        }
        self.api.submit_questionnaire(self._event_id, self._questionnaire_id, answers)

        # Verify
        self.api.get_my_status(self._event_id)
