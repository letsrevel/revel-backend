from decouple import config

# Feature flags
# Toggle platform capabilities via environment variables.

FEATURE_LLM_EVALUATION: bool = config("FEATURE_LLM_EVALUATION", default=True, cast=bool)
FEATURE_GOOGLE_SSO: bool = config("FEATURE_GOOGLE_SSO", default=True, cast=bool)
FEATURE_MALWARE_SCAN: bool = config("FEATURE_MALWARE_SCAN", default=True, cast=bool)
FEATURE_TELEGRAM: bool = config("FEATURE_TELEGRAM", default=True, cast=bool)
FEATURE_ORGANIZATION_CREATION: bool = config("FEATURE_ORGANIZATION_CREATION", default=True, cast=bool)
