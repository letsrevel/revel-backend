"""Guard against inline membership status filters (#1315).

Membership visibility/access rules live in ``events/utils/membership_access.py``. This AST walk
(no imports, no DB) fails when new code re-introduces an inline status filter outside the allowlist.

Rules (applied to ``filter``/``exclude``/``Q(...)`` calls only; ``create``/``update``/``get_or_create``
etc. are ignored, as are plain string literals such as GDPR export keys):

* (a) a keyword matching ``(organization_)?memberships__status(__in)?`` or ``(organization__)?members__id``;
* (b) ``filter``/``exclude`` on an expression containing ``OrganizationMember.objects`` with a
  ``status`` / ``status__in`` keyword;
* (c) a ``MembershipStatus.<X>`` attribute used as an argument (positional or keyword value, also
  nested in lists/tuples) of a ``filter``/``exclude``/``Q`` call.
"""

import ast
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]

_FILTER_CALLS = {"filter", "exclude"}
_A_KEYWORD = re.compile(r"^((organization_)?memberships__status(__in)?|(organization__)?members__id)$")
_B_KEYWORDS = {"status", "status__in"}

# Paths relative to src/ (prefix match). Each entry carries its reason.
ALLOWLIST: tuple[str, ...] = (
    "events/utils/membership_access.py",  # the predicates themselves
    "events/models/organization.py",  # model layer cannot import services/predicates; owns the field
    "events/utils/visibility.py",  # visibility helpers built on the membership status semantics
    "events/signals.py",  # signal handlers react to raw status transitions
    "events/service/subscription/",  # subscription lifecycle drives status transitions (not access reads)
    "events/service/organization_service/membership.py",  # membership CRUD: manages status directly
    "events/service/membership_manager/",  # membership state machine owns status semantics
    "events/service/permission_snapshot.py",  # snapshot builder needs raw per-status rows
    "events/controllers/me_subscriptions.py",  # user's own subscription listing, any status
    "events/service/blacklist_service.py",  # blacklist matching works on raw membership rows
    "events/admin/",  # admin lists and filters raw data
    "accounts/admin/",  # admin lists and filters raw data
    "events/management/",  # management commands operate on raw data
)


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _contains_member_objects(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Attribute)
        and n.attr == "objects"
        and isinstance(n.value, ast.Name)
        and n.value.id == "OrganizationMember"
        for n in ast.walk(node)
    )


def _is_membership_status_attr(node: ast.AST) -> bool:
    if not isinstance(node, ast.Attribute):
        return False
    owner = node.value
    return (isinstance(owner, ast.Name) and owner.id == "MembershipStatus") or (
        isinstance(owner, ast.Attribute) and owner.attr == "MembershipStatus"
    )


def find_violations(source: str, path: str) -> list[str]:
    """Return ``path:line`` entries for every inline membership status filter in ``source``."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        is_filter = name in _FILTER_CALLS
        if not (is_filter or name == "Q"):
            continue
        flagged = any(kw.arg and _A_KEYWORD.match(kw.arg) for kw in node.keywords)
        if is_filter and isinstance(node.func, ast.Attribute) and _contains_member_objects(node.func.value):
            flagged = flagged or any(kw.arg in _B_KEYWORDS for kw in node.keywords)
        if not flagged:
            arg_nodes: list[ast.AST] = [*node.args, *(kw.value for kw in node.keywords)]
            flagged = any(_is_membership_status_attr(n) for arg in arg_nodes for n in ast.walk(arg))
        if flagged:
            hits.append(f"{path}:{node.lineno}")
    return hits


def test_guard_detects_violation() -> None:
    """Each of rules (a), (b) and (c) is caught."""
    snippets = [
        "Event.objects.filter(organization_memberships__status='active')",  # (a)
        "OrganizationMember.objects.filter(user=u, status__in=statuses)",  # (b)
        "Q(user=u) & Q(role=OrganizationMember.MembershipStatus.ACTIVE)",  # (c)
    ]
    assert [len(find_violations(s, "x.py")) for s in snippets] == [1, 1, 1]


def test_guard_ignores_gdpr_string_keys() -> None:
    """Plain strings / dict keys / create calls are not filters."""
    source = (
        'RULES = {"organization_memberships": 1, "member_organizations": 2}\n'
        'x = "organization_memberships"\n'
        "OrganizationMember.objects.create(status=OrganizationMember.MembershipStatus.ACTIVE)\n"
    )
    assert find_violations(source, "x.py") == []


def test_no_inline_membership_status_filters() -> None:
    """No production module outside the allowlist filters on membership status inline."""
    violations: list[str] = []
    for file in sorted(_SRC.rglob("*.py")):
        rel = file.relative_to(_SRC).as_posix()
        parts = file.relative_to(_SRC).parts
        if "tests" in parts or "migrations" in parts or rel.startswith(ALLOWLIST):
            continue
        violations.extend(find_violations(file.read_text(), rel))
    assert not violations, "Inline membership status filters (use events.utils.membership_access):\n" + "\n".join(
        violations
    )
