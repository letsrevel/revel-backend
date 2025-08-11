import typing as t
from functools import lru_cache

import openai
from django.conf import settings
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_random_exponential


@lru_cache(maxsize=None)
def get_openai_client() -> openai.OpenAI:
    """Get a standard OpenAI client."""
    return openai.OpenAI(api_key=settings.OPENAI_API_KEY)


T = t.TypeVar("T", bound=BaseModel)


@retry(
    wait=wait_random_exponential(multiplier=1, max=40),
    stop=stop_after_attempt(6),
    reraise=True,
)
def call_openai(model: str, system_prompt: str, user_prompt: str, output_schema: t.Type[T]) -> T:
    """Thin wrapper around openai.responses.parse with retry & throttling.

    Returns a tuple of (parsed_response, LLMCallMeta).
    """
    client = get_openai_client()
    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        text_format=output_schema,
    )

    parsed_response = response.output_parsed
    assert parsed_response is not None

    # Create LLMCallMeta object
    # call_meta = schema.LLMCallMeta(generation_type=schema.GenerationType.TEXT, model=model, usage=response.usage)

    return parsed_response
