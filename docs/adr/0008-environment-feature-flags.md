# ADR-0008: Environment-Based Feature Flags

## Status

Accepted

## Context

Revel has features that need to be toggled per deployment — for example, LLM-powered
questionnaire evaluation (which requires an external API) and Google SSO (which
requires OAuth credentials). These capabilities should be easy to disable without
code changes, especially in development environments where the external dependencies
may not be available.

Several approaches were considered:

1. **Database-backed flags** (e.g., django-waffle): full runtime toggling via admin UI,
   but adds a DB dependency for every feature check and is overkill when we only have
   a handful of flags
2. **Third-party flag services** (LaunchDarkly, Unleash): powerful but adds external
   infrastructure and cost for a small number of flags
3. **Environment variables via `python-decouple`**: simple, zero-dependency (already
   used for all other settings), testable with `@override_settings`

## Decision

Feature flags are implemented as **Django settings** loaded from environment variables
in `src/revel/settings/features.py`:

```python
from decouple import config

FEATURE_LLM_EVALUATION: bool = config("FEATURE_LLM_EVALUATION", default=True, cast=bool)
FEATURE_GOOGLE_SSO: bool = config("FEATURE_GOOGLE_SSO", default=True, cast=bool)
```

Convention:

- All flags are prefixed with `FEATURE_` and use `bool` type
- Defaults are `True` (features enabled) unless there is a strong reason to opt-in
- Flags are checked via `django.conf.settings.FEATURE_*` in service and controller layers
- Tests override flags with `@override_settings(FEATURE_X=True/False)`

## Consequences

**Positive:**

- **Zero additional dependencies**: uses the existing `python-decouple` + Django settings
  infrastructure
- **Trivially testable**: `@override_settings` works out of the box
- **No runtime overhead**: flags are read once at startup, not per-request
- **Easy to discover**: all flags live in a single file

**Negative:**

- **Requires restart to change**: toggling a flag means restarting the Django process
- **Not suitable for gradual rollouts**: no percentage-based or user-targeted flagging

**Neutral:**

- If the number of flags grows significantly or we need runtime toggling, this decision
  should be revisited in favor of a database-backed solution
