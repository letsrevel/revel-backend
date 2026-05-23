"""Drift detection between PollQuestionController and QuestionnaireController.

The two controllers expose the same 15 question/section/option CRUD endpoints
under different URL prefixes (``/polls/{poll_id}/...`` vs
``/questionnaires/{org_questionnaire_id}/...``). The body of each route is a
thin pass-through to :class:`questionnaires.service.QuestionnaireService`, so
the duplication is intentional and small — but adding a new endpoint to one
side and forgetting the other is the kind of drift bug that's easy to ship.

This test introspects the registered ninja API and asserts the two sets are
identical after normalising the prefix. If a future contributor adds, removes,
or renames an endpoint on one side only, this fails with a clear diff.

See :class:`polls.controllers.question_controller.PollQuestionController` and
:class:`events.controllers.questionnaire.QuestionnaireController` for the
intent.
"""

import re

# Question/section/option CRUD path-shape patterns we care about.
#
# Both controllers also expose endpoints we DON'T want to compare:
# - QuestionnaireController: summary, submissions, evaluate, export,
#   event-series / event assignment, status, create-questionnaire.
# - PollQuestionController: pure CRUD subset only.
#
# We filter to the shared shape: paths whose tail matches one of these patterns.
_SHARED_PATH_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/sections$"),
    re.compile(r"^/sections/\{[^/]+\}$"),
    re.compile(r"^/multiple-choice-questions$"),
    re.compile(r"^/multiple-choice-questions/\{[^/]+\}$"),
    re.compile(r"^/multiple-choice-questions/\{[^/]+\}/options$"),
    re.compile(r"^/multiple-choice-options/\{[^/]+\}$"),
    re.compile(r"^/free-text-questions$"),
    re.compile(r"^/free-text-questions/\{[^/]+\}$"),
    re.compile(r"^/file-upload-questions$"),
    re.compile(r"^/file-upload-questions/\{[^/]+\}$"),
)


def _strip_parent_param(path: str, parent_param: str) -> str | None:
    """Strip a leading ``/{parent_param}`` segment from ``path``.

    Returns the remaining tail, or ``None`` when the path doesn't start with
    that parameter (so we skip non-question-CRUD endpoints like
    ``/polls/organizations/{organization_id}`` which doesn't begin with
    ``/{poll_id}``).
    """
    leading = "/{" + parent_param + "}"
    if not path.startswith(leading):
        return None
    return path[len(leading) :] or "/"


def _collect_question_crud_routes(prefix: str, parent_param: str) -> set[tuple[str, str]]:
    """Walk the registered API and return ``{(normalised_path, method), ...}``.

    ``parent_param`` is the path parameter name immediately under ``prefix``
    (e.g. ``poll_id`` or ``org_questionnaire_id``). It's stripped so the
    remaining tail can be compared across controllers.
    """
    from api.api import api

    found: set[tuple[str, str]] = set()
    for router_prefix, router in api._routers:
        if router_prefix != prefix:
            continue
        for path, path_view in router.path_operations.items():
            tail = _strip_parent_param(path, parent_param)
            if tail is None:
                continue
            if not any(pattern.match(tail) for pattern in _SHARED_PATH_SHAPES):
                continue
            for operation in path_view.operations:
                for method in operation.methods:
                    found.add((tail, method.upper()))
    return found


def test_polls_and_org_questionnaire_question_crud_in_sync() -> None:
    """The 15 question-CRUD shapes must exist on both controllers, identically.

    Adds, removes, or renames on one side only will surface here as a set
    difference with a readable diff.
    """
    poll_routes = _collect_question_crud_routes(prefix="/polls", parent_param="poll_id")
    org_routes = _collect_question_crud_routes(prefix="/questionnaires", parent_param="org_questionnaire_id")

    # Sanity: both sides must have something. Catches accidental empty matches
    # if the regexes drift out of date with the URL convention.
    assert poll_routes, "PollQuestionController exposes no recognised question-CRUD routes"
    assert org_routes, "QuestionnaireController exposes no recognised question-CRUD routes"

    only_in_polls = poll_routes - org_routes
    only_in_questionnaires = org_routes - poll_routes

    sections: list[str] = []
    if only_in_polls:
        sections.append(
            "Routes on PollQuestionController missing from QuestionnaireController:\n  "
            + "\n  ".join(f"{method:6} {path}" for path, method in sorted(only_in_polls))
        )
    if only_in_questionnaires:
        sections.append(
            "Routes on QuestionnaireController missing from PollQuestionController:\n  "
            + "\n  ".join(f"{method:6} {path}" for path, method in sorted(only_in_questionnaires))
        )
    assert not sections, "Question-CRUD endpoints drifted between controllers:\n\n" + "\n\n".join(sections)


def test_drift_test_actually_finds_routes() -> None:
    """Guard against the drift test silently matching zero routes on both sides.

    The shared-shape patterns are regex; if a future refactor changes the URL
    convention (e.g. renames ``multiple-choice-questions`` to ``mc-questions``)
    and forgets to update :data:`_SHARED_PATH_SHAPES`, the main test would
    pass with two empty sets. This test enforces that we always find at least
    the expected count of CRUD shapes and the expected HTTP methods.
    """
    poll_routes = _collect_question_crud_routes(prefix="/polls", parent_param="poll_id")
    methods_found = {method for _, method in poll_routes}
    assert {"POST", "PUT", "DELETE"}.issubset(methods_found), (
        f"Expected POST, PUT, and DELETE on PollQuestionController, found {sorted(methods_found)}"
    )
    # 15 declared endpoints: 5 POST (create), 5 PUT (update), 5 DELETE.
    assert len(poll_routes) >= 15, (
        f"Expected >= 15 question-CRUD routes on PollQuestionController, found {len(poll_routes)}: "
        f"{sorted(poll_routes)}"
    )
