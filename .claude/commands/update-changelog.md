---
description: Update CHANGELOG.md based on git tags, merged PRs, and actual code diffs
argument-hint: "[optional: version range, e.g. v1.48.1..v1.56.0, or 'unreleased']"
---

Invoke the `update-changelog` skill via the Skill tool and follow its workflow to update `CHANGELOG.md`.

Scope hint from the user (may be empty): $ARGUMENTS

If no scope was given, default to: reconcile the full gap between the latest version present in `CHANGELOG.md` and the latest git tag, then refresh `[Unreleased]` for any commits past the latest tag.

Do not run `make release`, edit `pyproject.toml`, or push tags — only edit `CHANGELOG.md`.
