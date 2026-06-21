from decouple import config

# Feature flags
# Product capability toggles read per deployment. See ADR-0008 for the taxonomy
# (FEATURE_* product capabilities here, vs. operational *_MODE / ENABLE_* toggles in
# their domain settings module, vs. runtime SiteSettings in the DB).

FEATURE_LLM_EVALUATION: bool = config("FEATURE_LLM_EVALUATION", default=True, cast=bool)
FEATURE_GOOGLE_SSO: bool = config("FEATURE_GOOGLE_SSO", default=False, cast=bool)
FEATURE_MALWARE_SCAN: bool = config("FEATURE_MALWARE_SCAN", default=True, cast=bool)
FEATURE_TELEGRAM: bool = config("FEATURE_TELEGRAM", default=True, cast=bool)
FEATURE_ORGANIZATION_CREATION: bool = config("FEATURE_ORGANIZATION_CREATION", default=True, cast=bool)

# Observability is a deployment capability, so it lives here under the FEATURE_ prefix.
# The legacy ``ENABLE_OBSERVABILITY`` env var is still honoured as a deprecated alias for
# one release (existing infra .env files / `make setup`): read FEATURE_OBSERVABILITY first
# and fall back to it. ``ENABLE_OBSERVABILITY`` (the settings attribute) is likewise kept
# as a deprecated alias for any out-of-tree consumers — prefer FEATURE_OBSERVABILITY.
FEATURE_OBSERVABILITY: bool = config(
    "FEATURE_OBSERVABILITY",
    default=config("ENABLE_OBSERVABILITY", default=True, cast=bool),
    cast=bool,
)
ENABLE_OBSERVABILITY: bool = FEATURE_OBSERVABILITY  # Deprecated alias — remove after one release.
