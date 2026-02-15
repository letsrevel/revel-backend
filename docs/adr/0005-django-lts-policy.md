# ADR-0005: Django 5.2 LTS Policy

## Status

Accepted

## Context

Django 6.x has been released with new features and improvements. However, Django 5.2
is designated as a **Long-Term Support (LTS)** release, receiving security updates
until **April 2028**.

The question: should we upgrade to Django 6.x for the latest features, or stay on
the stable LTS release?

Our priorities:

- **Stability**: The platform handles real events with real users -- regressions in
  core framework behavior are costly.
- **Security**: We need guaranteed security patches for the foreseeable future.
- **Ecosystem compatibility**: Third-party packages (Django Ninja, django-storages,
  etc.) need time to fully support new major versions.

## Decision

**Stay on Django 5.2 LTS.** Upgrade to a newer Django version only when there are
compelling reasons:

- Security CVEs not backported to 5.2
- Significant performance improvements needed for our workload
- Features that would substantially reduce complexity or enable key functionality
- Third-party ecosystem has fully stabilized on the new version

We do **not** upgrade for bleeding-edge features alone.

## Consequences

**Positive:**

- Stability and predictability -- 5.2 is battle-tested
- Security updates guaranteed until April 2028
- Full third-party package compatibility
- No risk of regressions from major version changes

**Negative:**

- Miss new Django 6.x features and improvements
- May accumulate migration debt if we wait too long

**Neutral:**

- Will revisit this decision when Django 6.x matures or when a clear, specific need
  arises
- The LTS window gives us ample time to plan an upgrade path
