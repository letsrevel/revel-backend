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
FEATURE_GOOGLE_SSO: bool = config("FEATURE_GOOGLE_SSO", default=False, cast=bool)  # opt-in
```

Convention:

- All flags are prefixed with `FEATURE_` and use `bool` type
- Defaults are `True` (features enabled) **unless the capability needs external credentials
  or setup to work at all** — those default `False` (opt-in), so a deployment that hasn't
  configured them doesn't advertise a broken feature
- Flags are checked via `django.conf.settings.FEATURE_*` in service and controller layers
- Tests override flags with `@override_settings(FEATURE_X=True/False)`

**Opt-in exception — `FEATURE_GOOGLE_SSO` defaults to `False`.** Google SSO is inert without
OAuth client credentials, so enabling it by default would surface a login button that 403s on
any instance (self-hosted or otherwise) that hasn't set up Google OAuth. Operators opt in with
`FEATURE_GOOGLE_SSO=True` once credentials are configured. All other current flags default
`True`.

Current `FEATURE_*` flags: `FEATURE_LLM_EVALUATION`, `FEATURE_GOOGLE_SSO`,
`FEATURE_MALWARE_SCAN`, `FEATURE_TELEGRAM`, `FEATURE_ORGANIZATION_CREATION`,
`FEATURE_OBSERVABILITY`.

## Taxonomy: which mechanism for which toggle

Not every switch is a product feature flag. To keep `features.py` meaningful and stop
operational/dev knobs from sprawling, a toggle belongs in exactly one of three places:

| Mechanism | What it is | Where it lives | Examples |
| --- | --- | --- | --- |
| **`FEATURE_*` env flag** | A product capability toggled per deployment | `src/revel/settings/features.py` | `FEATURE_LLM_EVALUATION`, `FEATURE_TELEGRAM`, `FEATURE_MALWARE_SCAN`, `FEATURE_ORGANIZATION_CREATION`, `FEATURE_OBSERVABILITY` |
| **Operational / dev toggle** | Profiling, throttling, test/CI behaviour, transport — *not* a product capability | its domain settings module (`base.py`, `email.py`, `celery.py`, `sso.py`, …) | `DEMO_MODE`, `SILK_PROFILER`, `DISABLE_THROTTLING`, `SYSTEM_TESTING`, `EMAIL_DRY_RUN`, `CELERY_TASK_ALWAYS_EAGER`, `SSO_SHOW_FORM_ON_ADMIN_PAGE` |
| **`SiteSettings` (DB singleton)** | Runtime, admin-editable, no redeploy | `events` app, edited via admin | `notify_user_joined`, `notify_organization_created`, `live_emails` |

Guidance:

- A new switch is a `FEATURE_*` flag **only** if it gates a product capability a deployment
  might legitimately want off (an optional integration, an external dependency, a per-instance
  policy). Everything else is operational config and stays in its domain module.
- `SSO_SHOW_FORM_ON_ADMIN_PAGE` is intentionally **not** a `FEATURE_*` flag: it is an admin-UI
  toggle, not a product capability, and stays in `sso.py`.
- DB-backed runtime toggles (`SiteSettings`) are a deliberately separate mechanism (no redeploy)
  and are out of scope for this ADR; do not migrate them into `features.py`.

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
