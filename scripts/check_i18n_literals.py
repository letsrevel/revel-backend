#!/usr/bin/env python3
"""Assert that user-facing error messages are marked for translation.

Two rendering paths put a raised message straight into the HTTP response body:

1. ``raise HttpError(<status>, <message>)`` — Ninja renders ``<message>`` as the
   ``detail`` of the error body.
2. ``raise <AppError>(<message>)`` where ``<AppError>`` is registered in some
   app's ``exception_handlers.HANDLERS`` map with ``make_simple_handler`` — that
   factory renders ``str(exc)`` as the ``detail`` (see
   ``common/exception_handlers.py``). Ninja Extra dispatches by MRO, so
   *subclasses* of a registered class are covered too, and its
   ``PermissionDenied`` behaves the same way.

A bare string literal (or an f-string, which ``xgettext`` cannot extract at all)
in either position ships untranslated English to every locale. This check
enforces the house idiom at ``make check`` time via the source AST — no imports,
no DB::

    raise HttpError(400, str(_("Something went wrong.")))
    raise PollValidationError(str(_("Unknown event {}.").format(event_id)))

Interpolation must translate *first* and then ``.format(...)``; f-strings are
invisible to the extractor and are therefore rejected outright.

Usage: ``python scripts/check_i18n_literals.py [src_dir]``  (default: ``src``)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Exception classes rendered to the client outside the per-app HANDLERS maps.
_EXTRA_RENDERED_EXCEPTIONS = {"HttpError", "PermissionDenied"}

# Directory names whose contents are never user-facing API responses.
_SKIPPED_DIRS = {"migrations", "tests"}


def _simple_handler_exceptions(src_dir: Path) -> set[str]:
    """Collect exception class names mapped with ``make_simple_handler``.

    Parses every ``<app>/exception_handlers.py`` and reads the ``HANDLERS``
    dict literal. Only ``make_simple_handler`` entries matter: those render
    ``str(exc)``, so the raise site's message reaches the client.
    ``make_static_handler`` entries ignore the raised message entirely.
    """
    names: set[str] = set(_EXTRA_RENDERED_EXCEPTIONS)
    for file in sorted(src_dir.glob("*/exception_handlers.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            mapping = _handlers_dict(node)
            if mapping is not None:
                names |= _simple_handler_keys(mapping)
    return names


def _handlers_dict(node: ast.AST) -> ast.Dict | None:
    """The dict literal assigned to the name ``HANDLERS``, if ``node`` is that assignment."""
    if not isinstance(node, ast.AnnAssign | ast.Assign):
        return None
    targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
    if not any(isinstance(tgt, ast.Name) and tgt.id == "HANDLERS" for tgt in targets):
        return None
    return node.value if isinstance(node.value, ast.Dict) else None


def _simple_handler_keys(mapping: ast.Dict) -> set[str]:
    """Names of the exception classes mapped with ``make_simple_handler``."""
    names: set[str] = set()
    for key, value in zip(mapping.keys, mapping.values, strict=True):
        if not isinstance(key, ast.Name) or not isinstance(value, ast.Call):
            continue
        if isinstance(value.func, ast.Name) and value.func.id == "make_simple_handler":
            names.add(key.id)
    return names


def _expand_with_subclasses(names: set[str], src_dir: Path) -> set[str]:
    """Grow ``names`` with every class in ``src`` that inherits from one of them.

    Ninja Extra dispatches exception handlers by MRO, so a subclass of a
    registered class is rendered by the same handler (e.g. the questionnaires
    ``File*Error`` family, all registered once via ``FileValidationError``).
    Bases are matched by bare name — good enough for a source-level lint.
    """
    bases_by_name: dict[str, set[str]] = {}
    for file in sorted(src_dir.rglob("*.py")):
        if _SKIPPED_DIRS & set(file.parts):
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
                bases_by_name.setdefault(node.name, set()).update(bases)
    resolved = set(names)
    changed = True
    while changed:
        changed = False
        for name, bases in bases_by_name.items():
            if name not in resolved and bases & resolved:
                resolved.add(name)
                changed = True
    return resolved


def _offending_argument(call: ast.Call, rendered: set[str]) -> ast.expr | None:
    """Return the literal message argument of ``call``, or ``None`` if it is fine."""
    func = call.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if name not in rendered:
        return None
    # HttpError's message is the SECOND positional argument; app exceptions take it first.
    index = 1 if name == "HttpError" else 0
    if len(call.args) <= index:
        return None
    arg = call.args[index]
    if isinstance(arg, ast.JoinedStr):
        return arg
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg
    return None


def find_violations(src_dir: Path) -> list[tuple[Path, int, str]]:
    """Return ``(file, lineno, reason)`` for every unmarked user-facing message."""
    rendered = _expand_with_subclasses(_simple_handler_exceptions(src_dir), src_dir)
    violations: list[tuple[Path, int, str]] = []
    for file in sorted(src_dir.rglob("*.py")):
        if _SKIPPED_DIRS & set(file.parts):
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            arg = _offending_argument(node.exc, rendered)
            if arg is None:
                continue
            func = node.exc.func
            exc_name = func.id if isinstance(func, ast.Name) else t_attr(func)
            kind = "f-string" if isinstance(arg, ast.JoinedStr) else "bare literal"
            violations.append((file, arg.lineno, f"{exc_name}(...) — {kind}"))
    return violations


def t_attr(func: ast.expr) -> str:
    """Best-effort dotted name for an attribute callee (for the report only)."""
    return func.attr if isinstance(func, ast.Attribute) else "<expr>"


def main() -> int:
    """Print any unmarked user-facing messages and return a non-zero exit code."""
    src_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "src").resolve()
    violations = find_violations(src_dir)
    if not violations:
        sys.stdout.write("✅ Every user-facing error message is marked for translation.\n")
        return 0

    lines = ["❌ Found user-facing error message(s) that bypass gettext:"]
    for file, lineno, reason in violations:
        rel = file.relative_to(src_dir.parent)
        lines.append(f"  - {rel}:{lineno}  {reason}")
    lines.append(
        '\nWrap the message: raise HttpError(400, str(_("...")))  — and for interpolation '
        'translate first, then format: str(_("... {}").format(value)). '
        "See revel-backend/CLAUDE.md and scripts/check_i18n_literals.py."
    )
    sys.stderr.write("\n".join(lines) + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
