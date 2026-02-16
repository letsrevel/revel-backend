import typing as t
from functools import lru_cache

import instructor
from django.conf import settings
from pydantic import BaseModel

T = t.TypeVar("T", bound=BaseModel)


def _resolve_instructor_mode(mode_str: str) -> instructor.Mode | None:
    """Resolve a mode string to an instructor.Mode enum, or None for auto-detection."""
    if not mode_str:
        return None
    return instructor.Mode[mode_str]


@lru_cache(maxsize=4)
def _get_instructor_client(
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    mode: str = "",
) -> instructor.Instructor:
    """Create and cache an Instructor client for the given provider/model.

    Cached to avoid creating a new HTTP client on every LLM call.

    Args:
        model: Model identifier including provider prefix.
        api_key: API key for the LLM provider.
        base_url: Override the provider's default base URL.
        mode: Instructor mode name (e.g. "JSON", "TOOLS"). Empty string
              for provider auto-detection.
    """
    # Omit falsy values (None, "") so providers use their defaults
    kwargs: dict[str, t.Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    resolved_mode = _resolve_instructor_mode(mode)
    if resolved_mode is not None:
        kwargs["mode"] = resolved_mode

    return t.cast(instructor.Instructor, instructor.from_provider(model, **kwargs))


def call_llm(
    model: str,
    system_prompt: str,
    user_prompt: str,
    output_schema: type[T],
    max_retries: int | None = None,
) -> T:
    """Provider-agnostic LLM call with validated structured output.

    Uses Instructor's from_provider() which supports OpenAI, Ollama, Anthropic,
    and many more. Connection settings (API key, base URL) are read from
    Django settings (see revel/settings/llm.py).

    Note: Instructor's max_retries handles retries on **validation failure**
    (when the LLM output doesn't match the Pydantic schema). It does NOT
    retry on network errors or rate limits (HTTP 429/500/503). For Celery
    tasks, Celery's own retry mechanism covers transport-level failures.

    Args:
        model: Model identifier including provider prefix
               (e.g. "openai/gpt-4o-mini", "ollama/llama3.1:8b").
        system_prompt: System message for the LLM.
        user_prompt: User message for the LLM.
        output_schema: Pydantic model for response validation.
        max_retries: Retries on validation failure. Defaults to settings.LLM_MAX_RETRIES.

    Returns:
        Validated instance of output_schema.
    """
    if max_retries is None:
        max_retries = settings.LLM_MAX_RETRIES

    client = _get_instructor_client(
        model=model,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        mode=settings.LLM_INSTRUCTOR_MODE,
    )
    return client.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_model=output_schema,
        max_retries=max_retries,
    )
