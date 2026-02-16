from decouple import config

# LLM Configuration for questionnaire evaluation.
# Uses Instructor (https://python.useinstructor.com/) with provider auto-detection.
# The model string includes the provider prefix: "provider/model-name".
#
# Local development (Ollama — default, no API key needed):
#   LLM_DEFAULT_MODEL=ollama/llama3.1:8b
#
# Production (OpenAI):
#   LLM_DEFAULT_MODEL=openai/gpt-4o-mini
#   LLM_API_KEY=sk-...
#
# See https://python.useinstructor.com/integrations/ for all supported providers.

# Model identifier including provider prefix
LLM_DEFAULT_MODEL: str = config("LLM_DEFAULT_MODEL", default="ollama/llama3.1:8b")

# API key for the LLM provider. Not needed for local providers like Ollama.
LLM_API_KEY: str | None = config("LLM_API_KEY", default=None) or None

# Override the provider's default base URL. Typically not needed —
# Instructor resolves the correct endpoint from the provider prefix.
LLM_BASE_URL: str | None = config("LLM_BASE_URL", default=None) or None

# Maximum retries on validation failure (handled by Instructor)
LLM_MAX_RETRIES: int = config("LLM_MAX_RETRIES", default=3, cast=int)

# Instructor mode for structured output extraction.
# Instructor auto-detects based on provider, but smaller Ollama models
# (e.g. llama3.1:8b) don't handle TOOLS mode reliably — use JSON instead.
# Valid values: TOOLS, JSON, JSON_SCHEMA, MD_JSON (see instructor.Mode).
# Set to empty string to use Instructor's provider-based auto-detection.
LLM_INSTRUCTOR_MODE: str = config("LLM_INSTRUCTOR_MODE", default="JSON")
