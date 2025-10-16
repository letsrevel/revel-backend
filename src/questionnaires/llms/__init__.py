from .llm_backends import (
    TRANSFORMERS_AVAILABLE,
    BetterChatGPTEvaluator,
    IntermediateChatGPTEvaluator,
    MockEvaluator,
    SanitizingChatGPTEvaluator,
    VulnerableChatGPTEvaluator,
)

__all__ = [
    "MockEvaluator",
    "VulnerableChatGPTEvaluator",
    "IntermediateChatGPTEvaluator",
    "BetterChatGPTEvaluator",
    "SanitizingChatGPTEvaluator",
    "TRANSFORMERS_AVAILABLE",
]

# Conditionally import SentinelChatGPTEvaluator only if transformers is available
if TRANSFORMERS_AVAILABLE:
    from .llm_backends import SentinelChatGPTEvaluator as SentinelChatGPTEvaluator  # noqa: F401

    __all__.append("SentinelChatGPTEvaluator")
