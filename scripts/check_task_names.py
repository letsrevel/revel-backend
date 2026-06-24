#!/usr/bin/env python3
"""Assert that every Celery task declares an explicit ``name=``.

A bare ``@shared_task`` (or ``@app.task``) takes Celery's *default* name, derived
from the task's module path (``<module>.<func>``). That couples the registered
task name to the file location, so **moving or renaming the module silently
changes the task's name** — which breaks ``django_celery_beat`` ``PeriodicTask``
rows (they reference tasks by name string) and strands in-flight messages across
a deploy.

Pinning ``name=`` decouples the registered name from the file path, making task
modules safe to move/split. This check enforces that policy at ``make check`` time
via the source AST (no imports, no DB).

Usage: ``python scripts/check_task_names.py [src_dir]``  (default: ``src``)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Decorator callee names treated as Celery task decorators.
_TASK_DECORATOR_NAMES = {"shared_task", "task", "periodic_task"}


def _is_task_decorator(node: ast.expr) -> bool:
    """True if a decorator expression registers a Celery task."""
    # @shared_task  /  @app.task
    if isinstance(node, ast.Name):
        return node.id == "shared_task"
    if isinstance(node, ast.Attribute):
        return node.attr in {"task", "periodic_task"}
    # @shared_task(...)  /  @app.task(...)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in _TASK_DECORATOR_NAMES
        if isinstance(func, ast.Attribute):
            return func.attr in {"task", "periodic_task"}
    return False


def _decorator_has_name_kwarg(node: ast.expr) -> bool:
    """True if a task decorator passes an explicit ``name=`` keyword."""
    if not isinstance(node, ast.Call):
        return False  # bare @shared_task / @app.task — never has a name
    return any(kw.arg == "name" for kw in node.keywords)


def _module_path(file: Path, src_dir: Path) -> str:
    """Dotted module path for ``file`` relative to ``src_dir`` (e.g. ``geo.tasks``)."""
    rel = file.relative_to(src_dir).with_suffix("")
    return ".".join(rel.parts)


def find_violations(src_dir: Path) -> list[tuple[Path, int, str, str]]:
    """Return ``(file, lineno, func_name, suggested_name)`` for every unpinned task."""
    violations: list[tuple[Path, int, str, str]] = []
    for file in sorted(src_dir.rglob("*.py")):
        parts = set(file.parts)
        if "migrations" in parts or "tests" in parts:
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except SyntaxError:
            continue
        module = _module_path(file, src_dir)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in func.decorator_list:
                if _is_task_decorator(dec) and not _decorator_has_name_kwarg(dec):
                    violations.append((file, func.lineno, func.name, f"{module}.{func.name}"))
    return violations


def main() -> int:
    """Print any unpinned tasks and return a non-zero exit code if found."""
    src_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "src").resolve()
    violations = find_violations(src_dir)
    if not violations:
        sys.stdout.write("✅ Every Celery task declares an explicit name=.\n")
        return 0

    lines = ["❌ Found Celery task(s) without an explicit name= (bare default name is move-unsafe):"]
    for file, lineno, func_name, suggested in violations:
        rel = file.relative_to(src_dir.parent)
        lines.append(f'  - {rel}:{lineno}  {func_name}()  →  add name="{suggested}"')
    lines.append(
        "\nPin the task's current default name (shown above) so the registered name stays "
        "byte-identical while the module becomes safe to move. See revel-backend/CLAUDE.md "
        "(Celery task naming)."
    )
    sys.stderr.write("\n".join(lines) + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
