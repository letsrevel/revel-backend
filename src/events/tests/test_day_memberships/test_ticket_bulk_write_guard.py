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
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3]

# Paths relative to src/ (prefix match). Each entry says how the site keeps the hook honest.
ALLOWLIST: dict[str, str] = {
    "events/service/batch_ticket_service/tickets.py": "bulk_create; calls day_membership_service.on_tickets_issued",
    "events/service/series_pass_service.py": (
        "bulk_create in materialize_tickets and the PENDING->ACTIVE bulk_update in confirm_held_pass_payment; "
        "both call day_membership_service.on_tickets_issued"
    ),
    "events/tasks/refunds.py": "bulk event cancellation .update(status=CANCELLED); calls on_ticket_status_change",
    "events/service/ticket_file_service.py": (
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


def find_bulk_ticket_writes(source: str, path: str) -> list[str]:
    """Return ``path:line`` entries for every bulk Ticket write that can change status or create rows."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
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
            continue
        if flagged and _contains_ticket_objects(node.func.value):
            hits.append(f"{path}:{node.lineno}")
    return hits


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


def test_no_unlisted_bulk_ticket_writes_in_src() -> None:
    """No production module outside the allowlist bulk-writes Ticket status or rows."""
    violations: list[str] = []
    scanned = 0
    for file in sorted(_SRC.rglob("*.py")):
        parts = file.relative_to(_SRC).parts
        rel = "/".join(parts)
        if {"tests", "migrations"} & set(parts[:-1]) or ("management" in parts and "commands" in parts):
            continue
        if rel in ALLOWLIST:
            continue
        scanned += 1
        violations.extend(find_bulk_ticket_writes(file.read_text(), rel))
    assert scanned > 100, f"guard scanned only {scanned} files; the walk is probably broken"
    assert not violations, "Bulk Ticket writes bypass the day-membership hook:\n" + "\n".join(violations)


def test_allowlisted_files_exist_and_call_the_service() -> None:
    """An allowlist entry that points nowhere, or at a file that no longer calls the hook, is stale."""
    for rel, reason in ALLOWLIST.items():
        source = (_SRC / rel).read_text()
        if "calls " in reason:
            assert "day_membership_service" in source, f"{rel} no longer calls day_membership_service"
        assert find_bulk_ticket_writes(source, rel), f"{rel} no longer bulk-writes tickets; drop it from ALLOWLIST"
