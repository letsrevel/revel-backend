from django.apps import AppConfig


class CommonConfig(AppConfig):
    """Configuration for the common app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "common"

    def ready(self) -> None:
        """Initialize app-level services.

        Called once Django is fully loaded.
        """
        # Import and initialize observability
        from common.observability import init_profiling, init_tracing

        init_tracing()
        init_profiling()

        # Importing the module runs its @register() decorators.
        import common.checks  # noqa: F401

        _install_fallback_token_backend()


def _install_fallback_token_backend() -> None:
    """Swap ninja-jwt's module-level backend for one that honours ``SECRET_KEY_FALLBACKS``.

    The backend is not configurable (``Token.token_backend`` imports ``ninja_jwt.state.token_backend``
    lazily), so it is replaced here with the same constructor arguments the module itself uses.
    """
    import ninja_jwt.state as state
    from ninja_jwt.settings import api_settings

    from common.jwt_backend import FallbackTokenBackend

    state.token_backend = FallbackTokenBackend(
        api_settings.ALGORITHM,
        api_settings.SIGNING_KEY,
        api_settings.VERIFYING_KEY,
        api_settings.AUDIENCE,
        api_settings.ISSUER,
        api_settings.JWK_URL,  # type: ignore[arg-type]  # ninja_jwt types it as AnyUrl, the backend takes the raw value
        api_settings.LEEWAY,
        api_settings.JSON_ENCODER,
    )
