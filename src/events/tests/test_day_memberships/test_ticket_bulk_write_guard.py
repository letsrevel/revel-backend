"""Guard against bulk Ticket status writes that skip the day-membership hook (#1320).

Bulk writes (``QuerySet.update``, ``bulk_update``, ``bulk_create``) skip ``save()`` and therefore the
Ticket ``post_save`` receiver that grants / revokes day memberships. This AST walk (no imports, no DB)
fails when production code bulk-writes Ticket rows without calling ``day_membership_service``.

Rules (applied only when the receiver expression contains ``Ticket.objects``):

* ``update(...)`` with a ``status`` keyword, or a ``**mapping`` that is a dict literal containing
  ``status`` or any non-literal expression (cannot be proven safe);
* ``bulk_create(...)`` regardless of arguments; ``bulk_update(...)`` when the field list names
  ``"status"`` or is not a literal list.

Sibling of ``test_member_register/test_bulk_write_guard.py``. Scope limit: querysets assigned to a variable
before ``.update()`` are not detected.
"""
# ponytail: the receiver must be written inline (``Ticket.objects...update``). Tracking variable aliases
# would need data-flow analysis; upgrade path is to forbid the alias pattern instead.

import ast
import typing as t
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3]

# (path relative to src/, enclosing function name) -> how that site keeps the hook honest.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("events/service/batch_ticket_service/tickets.py", "create_tickets"): (
        "bulk_create; calls day_membership_service.on_tickets_issued"
    ),
    ("events/service/series_pass_service.py", "materialize_tickets"): (
        "bulk_create; calls day_membership_service.on_tickets_issued"
    ),
    ("events/service/series_pass_service.py", "confirm_held_pass_payment"): (
        "PENDING->ACTIVE bulk_update; calls day_membership_service.on_tickets_issued with old_status=PENDING"
    ),
    ("events/tasks/refunds.py", "refund_one_cancelled_event_ticket"): (
        "bulk event cancellation .update(status=CANCELLED); calls day_membership_service.on_ticket_status_change"
    ),
    ("events/service/ticket_file_service.py", "_persist_and_update"): (
        "**update_fields carries only pdf_file/pkpass_file/file_content_hash, never status"
    ),
}


def _contains_ticket_objects(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Attribute)
            and n.attr in {"objects", "_default_manager"}
            and (
                (isinstance(n.value, ast.Name) and n.value.id == "Ticket")
                or (isinstance(n.value, ast.Attribute) and n.value.attr == "Ticket")
            )
        ):
            return True
    return False


def _unpacked_kwargs_flagged(value: ast.expr) -> bool:
    if not isinstance(value, ast.Dict):
        return True
    return any(k is None or not isinstance(k, ast.Constant) or k.value == "status" for k in value.keys)


def _bulk_update_flagged(node: ast.Call) -> bool:
    fields = (
        node.args[1] if len(node.args) > 1 else next((kw.value for kw in node.keywords if kw.arg == "fields"), None)
    )
    if not isinstance(fields, (ast.List, ast.Tuple)):
        return True
    return any(not isinstance(e, ast.Constant) or e.value == "status" for e in fields.elts)


def _flagged(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False
    name = node.func.attr
    if name == "bulk_create":
        flagged = True
    elif name == "bulk_update":
        flagged = _bulk_update_flagged(node)
    elif name == "update":
        flagged = any(
            kw.arg == "status" if kw.arg is not None else _unpacked_kwargs_flagged(kw.value) for kw in node.keywords
        )
    else:
        return False
    return flagged and _contains_ticket_objects(node.func.value)


class _Finder(ast.NodeVisitor):
    """Collect ``(line, enclosing function name)`` of flagged calls; module level counts as ``<module>``."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.hits: list[tuple[int, str]] = []

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Call(self, node: ast.Call) -> None:
        if _flagged(node):
            self.hits.append((node.lineno, self.stack[-1] if self.stack else "<module>"))
        self.generic_visit(node)


def find_bulk_ticket_writes(source: str, path: str) -> list[tuple[str, int, str]]:
    """Return ``(path, line, enclosing function)`` for every bulk Ticket write that can change status or create rows."""
    finder = _Finder()
    finder.visit(ast.parse(source))
    return [(path, line, func) for line, func in finder.hits]


def test_guard_detects_bulk_writes() -> None:
    """Each bulk write shape is caught exactly once."""
    snippets = [
        "Ticket.objects.filter(x=1).update(status=s)",
        "models.Ticket.objects.filter(x=1).update(status=s)",
        'Ticket.objects.filter(x=1).update(**{"status": s})',
        "Ticket.objects.filter(x=1).update(**fields)",
        "Ticket.objects.bulk_create(rows)",
        'Ticket.objects.bulk_update(rows, ["status", "price_paid"])',
        "Ticket.objects.bulk_update(rows, fields)",
        "Ticket._default_manager.filter(x=1).update(status=s)",
    ]
    assert [len(find_bulk_ticket_writes(s, "x.py")) for s in snippets] == [1] * len(snippets)


def test_guard_ignores_unrelated() -> None:
    """Non-status updates and other models' bulk writes are not flagged."""
    snippets = [
        "Ticket.objects.filter(x=1).update(pdf_file='')",
        'Ticket.objects.filter(x=1).update(**{"pdf_file": ""})',
        'Ticket.objects.bulk_update(rows, ["ticket_number", "issued_at"])',
        "TicketTier.objects.filter(x=1).update(status=s)",
        "OrganizationMember.objects.bulk_create(rows)",
    ]
    assert [find_bulk_ticket_writes(s, "x.py") for s in snippets] == [[]] * len(snippets)


def test_guard_reports_the_enclosing_function() -> None:
    """Allowlisting is per function, so a second write in the same file is not covered by the first."""
    source = (
        "def a():\n    Ticket.objects.bulk_create(r)\n\n"
        "class C:\n    def b(self):\n        Ticket.objects.bulk_create(r)\n"
    )
    assert [func for _, _, func in find_bulk_ticket_writes(source, "x.py")] == ["a", "b"]


def _functions_calling_day_membership_service(source: str) -> set[str]:
    """Names of functions whose own body references ``day_membership_service.<attr>``."""
    return {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(
            isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "day_membership_service"
            for n in ast.walk(node)
        )
    }


def _production_files() -> t.Iterator[tuple[str, Path]]:
    for file in sorted(_SRC.rglob("*.py")):
        parts = file.relative_to(_SRC).parts
        if {"tests", "migrations"} & set(parts[:-1]) or ("management" in parts and "commands" in parts):
            continue
        yield "/".join(parts), file


def test_no_unlisted_bulk_ticket_writes_in_src() -> None:
    """No production function outside the allowlist bulk-writes Ticket status or rows."""
    violations: list[str] = []
    scanned = 0
    for rel, file in _production_files():
        scanned += 1
        violations.extend(
            f"{path}:{line} in {func}()"
            for path, line, func in find_bulk_ticket_writes(file.read_text(), rel)
            if (path, func) not in ALLOWLIST
        )
    assert scanned > 100, f"guard scanned only {scanned} files; the walk is probably broken"
    assert not violations, "Bulk Ticket writes bypass the day-membership hook:\n" + "\n".join(violations)


def test_allowlist_entries_are_live_and_hooked() -> None:
    """An entry that points at a function that no longer bulk-writes tickets, or lost its hook, is stale."""
    found = {
        (rel, func)
        for rel, file in _production_files()
        for _, _, func in find_bulk_ticket_writes(file.read_text(), rel)
    }
    for (rel, func), reason in ALLOWLIST.items():
        assert (rel, func) in found, f"{rel}:{func} no longer bulk-writes tickets; drop it from ALLOWLIST"
        if "calls " in reason:
            assert func in _functions_calling_day_membership_service((_SRC / rel).read_text()), (
                f"{rel}:{func} no longer calls day_membership_service"
            )
