# Post-Mortems

Post-mortems document security incidents, production bugs, and outages that
affected users. Each record describes the **discovery**, **root cause**,
**impact**, **fix**, and **lessons learned**.

We number them sequentially, mirroring the ADR convention.

---

## Template

When adding a new post-mortem, use this template:

```markdown
# PM-NNNN: Title

## Summary

One-paragraph description of what happened.

## Detection

How was the issue discovered?

## Timeline

Chronological sequence of events.

## Root Cause

Technical explanation of why the bug existed.

## Impact

Who and what was affected.

## Resolution

What was done to fix it.

## Lessons Learned

What we take away from this incident.

## References

Links to issues, PRs, and related resources.
```

---

## Index

| PM | Title | Date | Severity |
|---|---|---|---|
| [PM-0001](0001-guest-user-verification-bypass.md) | Guest User Verification Bypass | 2026-02-19 | High |
| [PM-0002](0002-server-side-cursor-pgbouncer.md) | Server-Side Cursors Orphaned Under PgBouncer Transaction Pooling | 2026-05-25 | Medium |

---

## Adding a New Post-Mortem

1. Determine the next sequential number (e.g., `0002`).
2. Create a new file: `docs/postmortems/NNNN-short-slug.md`.
3. Use the template above.
4. Add an entry to the index table in this file.
