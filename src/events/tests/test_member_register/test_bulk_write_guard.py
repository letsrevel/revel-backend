"""Guard against bulk writes to OrganizationMember status/tier (#1318).

Bulk writes (``QuerySet.update``, ``bulk_update``, ``bulk_create``) skip ``save()`` and therefore
skip the post_save signal that mirrors OrganizationMember transitions into the member register.
This AST walk (no imports, no DB) fails when production code bulk-writes those fields.

Rules (applied only when the receiver expression contains ``OrganizationMember.objects``):

* ``update(...)`` with a ``status``, ``tier`` or ``tier_id`` keyword;
* ``bulk_update(...)`` and ``bulk_create(...)`` regardless of arguments.

May merge with the #1315 membership-access guard (#1325).

Scope limit: querysets assigned to a variable before ``.update()`` are not detected.
# ponytail: the receiver must be written inline (``OrganizationMember.objects...update``). Tracking
# variable aliases would need data-flow analysis; upgrade path is to forbid the alias pattern instead.
"""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3]

_UPDATE_KEYWORDS = {"status", "tier", "tier_id"}
_ALWAYS_FLAGGED = {"bulk_update", "bulk_create"}

# Paths relative to src/ (prefix match). Each entry carries its reason. Empty: no bulk member writes exist.
ALLOWLIST: tuple[str, ...] = ()


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


def find_bulk_member_writes(source: str, path: str) -> list[str]:
    """Return ``path:line`` entries for every bulk OrganizationMember status/tier write in ``source``."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        name = node.func.attr
        if name in _ALWAYS_FLAGGED:
            flagged = True
        elif name == "update":
            flagged = any(kw.arg in _UPDATE_KEYWORDS for kw in node.keywords)
        else:
            continue
        if flagged and _contains_member_objects(node.func.value):
            hits.append(f"{path}:{node.lineno}")
    return hits


def test_guard_detects_bulk_writes() -> None:
    """Each bulk status/tier write shape is caught exactly once."""
    snippets = [
        "OrganizationMember.objects.filter(x=1).update(status=s)",
        "models.OrganizationMember.objects.filter(x=1).update(tier=t)",
        "OrganizationMember.objects.filter(x=1).update(tier_id=i)",
        'OrganizationMember.objects.bulk_update(rows, ["status"])',
        "OrganizationMember.objects.bulk_create(rows)",
    ]
    assert [len(find_bulk_member_writes(s, "x.py")) for s in snippets] == [1] * len(snippets)


def test_guard_ignores_unrelated() -> None:
    """Non-status updates and other models' status writes are not flagged."""
    snippets = [
        "OrganizationMember.objects.filter(x=1).update(updated_at=n)",
        "OrganizationMembershipRequest.objects.filter(pk=1).update(status=s)",
        "Ticket.objects.filter(x=1).update(status=s)",
    ]
    assert [find_bulk_member_writes(s, "x.py") for s in snippets] == [[], [], []]


def test_no_bulk_member_writes_in_src() -> None:
    """No production module outside the allowlist bulk-writes OrganizationMember status/tier."""
    violations: list[str] = []
    for file in sorted(_SRC.rglob("*.py")):
        rel = file.relative_to(_SRC).as_posix()
        if any(part in {"tests", "migrations"} for part in file.relative_to(_SRC).parts[:-1]):
            continue
        if "/management/commands/" in f"/{rel}" or rel.startswith(ALLOWLIST):
            continue
        violations.extend(find_bulk_member_writes(file.read_text(), rel))
    assert not violations, "Bulk OrganizationMember status/tier writes bypass the register mirror:\n" + "\n".join(
        violations
    )
