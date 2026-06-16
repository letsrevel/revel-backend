#!/usr/bin/env python3
"""Static QA gate for translation source (.po) files.

This replaces the old ".mo freshness" check (see ADR-0011, which reverts ADR-0006).
Compiled ``.mo`` files are no longer committed; they are built in the Docker image.
What matters for quality is the *source* (``.po``) being complete and in sync with
the code, which this script verifies — without needing compiled binaries.

Two independent checks, both must pass:

1. **Keys extracted** — re-run ``makemessages`` and fail if the code contains any
   ``_()``-wrapped string that is *missing* from the catalog. We compare the set of
   ``msgid``s (normalised, so wrapping/ordering is irrelevant), NOT the raw file
   text: gettext reflows ``.po`` differently across versions, so a textual
   ``git diff`` would report thousands of spurious changes on a version mismatch.
   The ``.po`` files are snapshotted in memory and restored afterwards, so a
   developer's in-progress edits are never clobbered.

2. **Keys translated** — parse every ``.po`` and fail on any entry that is ``fuzzy``
   or has an empty ``msgstr`` / ``msgstr[n]`` (an untranslated key).

Usage: run from the backend repo root::

    python scripts/check_translations.py

Check 1 requires the ``gettext`` toolchain (``xgettext``/``msgmerge``) on PATH.
"""

# ruff: noqa: T201  -- this is a CLI; printing the report to stdout is the whole point.

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

LANGUAGES = ["de", "it", "fr"]
SRC_DIR = Path(__file__).resolve().parent.parent / "src"


def _po_files() -> list[Path]:
    return sorted((SRC_DIR / "locale").glob("*/LC_MESSAGES/django.po"))


@dataclass
class Entry:
    """A single parsed .po entry."""

    msgctxt: str | None = None
    msgid: str = ""
    msgid_plural: str | None = None
    msgstrs: list[str] = field(default_factory=list)
    is_fuzzy: bool = False

    @property
    def is_header(self) -> bool:
        """True for the catalog header entry (empty msgid, no context)."""
        return self.msgid == "" and self.msgctxt is None

    @property
    def key(self) -> tuple[str | None, str]:
        """Identity of the entry: (msgctxt, msgid)."""
        return (self.msgctxt, self.msgid)

    @property
    def is_untranslated(self) -> bool:
        """True if every msgstr / msgstr[n] is empty."""
        return bool(self.msgstrs) and all(s == "" for s in self.msgstrs)


def _unquote(rest: str) -> str:
    """Extract the string content from the part of a line after a keyword."""
    rest = rest.strip()
    if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
        return rest[1:-1]
    return rest


def parse_po(text: str) -> list[Entry]:  # noqa: C901
    """Parse a .po file into entries, concatenating multi-line strings.

    Obsolete (``#~``) entries are ignored. Escapes are left as-is — we only test
    equality and emptiness, so consistent treatment is sufficient.
    """
    entries: list[Entry] = []
    entry = Entry()
    target: str | None = None  # which field a bare "..." continuation appends to
    started = False

    def flush() -> None:
        nonlocal entry, target, started
        if started:
            entries.append(entry)
        entry = Entry()
        target = None
        started = False

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if line.strip() == "":
            flush()
            continue
        if line.startswith("#~"):  # obsolete entry line
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                entry.is_fuzzy = True
                started = True
            target = None
            continue
        if line.startswith("msgctxt "):
            entry.msgctxt = _unquote(line[len("msgctxt ") :])
            target = "msgctxt"
            started = True
        elif line.startswith("msgid_plural "):
            entry.msgid_plural = _unquote(line[len("msgid_plural ") :])
            target = "msgid_plural"
            started = True
        elif line.startswith("msgid "):
            entry.msgid = _unquote(line[len("msgid ") :])
            target = "msgid"
            started = True
        elif line.startswith("msgstr["):
            _, _, rest = line.partition(" ")
            entry.msgstrs.append(_unquote(rest))
            target = "msgstr"
            started = True
        elif line.startswith("msgstr "):
            entry.msgstrs.append(_unquote(line[len("msgstr ") :]))
            target = "msgstr"
            started = True
        elif line.startswith('"'):  # continuation of the current field
            chunk = _unquote(line)
            if target == "msgctxt" and entry.msgctxt is not None:
                entry.msgctxt += chunk
            elif target == "msgid_plural" and entry.msgid_plural is not None:
                entry.msgid_plural += chunk
            elif target == "msgid":
                entry.msgid += chunk
            elif target == "msgstr" and entry.msgstrs:
                entry.msgstrs[-1] += chunk
    flush()
    return entries


def _key_set(entries: list[Entry]) -> set[tuple[str | None, str]]:
    return {e.key for e in entries if not e.is_header}


def check_keys_extracted() -> bool:
    """Re-extract messages and fail if any code string is missing from the catalog.

    Compares msgid *sets* (version-independent) and restores the .po files to their
    original bytes afterwards, so no working-tree changes leak out.
    """
    snapshots = {po: po.read_bytes() for po in _po_files()}
    committed = {po: _key_set(parse_po(data.decode("utf-8"))) for po, data in snapshots.items()}

    cmd = ["python", "manage.py", "makemessages", "--no-location", "--no-obsolete"]
    for lang in LANGUAGES:
        cmd += ["-l", lang]
    try:
        subprocess.run(cmd, cwd=SRC_DIR, check=True, capture_output=True, text=True)
        fresh = {po: _key_set(parse_po(po.read_text(encoding="utf-8"))) for po in _po_files()}
    finally:
        for po, data in snapshots.items():
            po.write_bytes(data)

    ok = True
    for po in _po_files():
        lang = po.parent.parent.name
        missing = sorted(k[1] for k in fresh[po] - committed[po])
        stale = sorted(k[1] for k in committed[po] - fresh[po])
        if missing:
            ok = False
            print(f"❌ [{lang}] {len(missing)} string(s) in the code are missing from the catalog:")
            for msgid in missing:
                print(f"   (missing) {msgid}")
            print("   Run 'make makemessages', translate the new strings, and commit the .po files.")
        if stale:
            print(f"⚠️  [{lang}] {len(stale)} catalog entries no longer appear in the code:")
            for msgid in stale:
                print(f"   (stale) {msgid}")
            print("   Run 'make makemessages' to prune them (not a failure).")
    if ok:
        print("✅ Translation keys are fully extracted (every code string is in the catalog).")
    return ok


def check_keys_translated() -> bool:
    """Fail if any entry is fuzzy or has an empty translation."""
    ok = True
    for po in _po_files():
        lang = po.parent.parent.name
        fuzzy: list[str] = []
        empty: list[str] = []
        for entry in parse_po(po.read_text(encoding="utf-8")):
            if entry.is_header:
                continue
            if entry.is_fuzzy:
                fuzzy.append(entry.msgid)
            elif entry.is_untranslated:
                empty.append(entry.msgid)
        if fuzzy or empty:
            ok = False
            print(f"❌ [{lang}] {len(empty)} untranslated, {len(fuzzy)} fuzzy entries:")
            for msgid in empty:
                print(f"   (empty) {msgid}")
            for msgid in fuzzy:
                print(f"   (fuzzy) {msgid}")
    if ok:
        print("✅ All translation keys are translated (no empty or fuzzy entries).")
    return ok


def main() -> int:
    """Run both checks and return a process exit code (0 = pass)."""
    extracted = check_keys_extracted()
    translated = check_keys_translated()
    return 0 if extracted and translated else 1


if __name__ == "__main__":
    sys.exit(main())
