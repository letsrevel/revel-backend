"""Guard against inline membership status filters (#1315).

Membership visibility/access rules live in ``events/utils/membership_access.py``. This AST walk
(no imports, no DB) fails when new code re-introduces an inline status filter outside the allowlist.

Rules (applied to ``filter``/``exclude``/``Q(...)`` calls only; ``create``/``update``/``get_or_create``
etc. are ignored, as are plain string literals such as GDPR export keys):

* (a) a keyword whose name contains ``memberships__status``, ``memberships__status__in``,
  ``organization_memberships__status`` / ``__in`` or ``members__id`` as a whole ``__``-separated
  suffix (matched with ``re.search`` on ``(^|__)…$``, so ``organization__members__id`` and
  ``memberships__status__in`` both count);
* (b) ``filter``/``exclude`` on an expression containing ``OrganizationMember.objects`` with a
  ``status`` / ``status__in`` keyword;
* (c) a ``MembershipStatus.<X>`` attribute used as an argument (positional or keyword value, also
  nested in lists/tuples) of a ``filter``/``exclude``/``Q`` call.

Scope limit: Python-side comparisons (``status == MembershipStatus.ACTIVE``) are not detected; use the
``is_*_status`` helpers there.
"""

import ast
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]

_FILTER_CALLS = {"filter", "exclude"}
_A_KEYWORD = re.compile(r"(^|__)((organization_)?memberships__status(__in)?|members__id)$")
_A_M2M_KEYWORD = re.compile(r"(^|__)members(__(id|pk|in))?$")
_B_KEYWORDS = {"status", "status__in"}

# Paths relative to src/ (prefix match). Each entry carries its reason.
ALLOWLIST: tuple[str, ...] = (
    "events/utils/membership_access.py",  # the predicates themselves
    "events/service/organization_service/membership.py",  # membership CRUD: manages status directly
    "events/service/permission_snapshot.py",  # snapshot builder needs raw per-status rows
    "events/service/blacklist_service.py",  # blacklist matching works on raw membership rows
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
        and (
            (isinstance(n.value, ast.Name) and n.value.id == "OrganizationMember")
            or (isinstance(n.value, ast.Attribute) and n.value.attr == "OrganizationMember")
        )
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
        flagged = any(kw.arg and (_A_KEYWORD.search(kw.arg) or _A_M2M_KEYWORD.search(kw.arg)) for kw in node.keywords)
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
        "Event.objects.filter(organization__memberships__status__in=s)",  # (a) join from Event
        "Ticket.objects.filter(user__organization_memberships__status=s)",  # (a) join from Ticket
        "Event.objects.filter(organization__members__id=1)",  # (a) members__id
        "models.OrganizationMember.objects.filter(status=s)",  # (b) attribute-qualified model
        "Organization.objects.filter(members=x)",  # (a) bare M2M
        "Event.objects.filter(organization__members=x)",  # (a) bare M2M join
        "Organization.objects.filter(members__in=x)",  # (a) bare M2M __in
        "Organization.objects.filter(members__pk=x)",  # (a) bare M2M __pk
    ]
    assert [len(find_violations(s, "x.py")) for s in snippets] == [1] * len(snippets)
    assert find_violations("Organization.objects.filter(staff_members__id=1)", "x.py") == []


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
