from decouple import config

# Feature flags
# Toggle platform capabilities via environment variables.

FEATURE_LLM_EVALUATION: bool = config("FEATURE_LLM_EVALUATION", default=True, cast=bool)
FEATURE_GOOGLE_SSO: bool = config("FEATURE_GOOGLE_SSO", default=True, cast=bool)
