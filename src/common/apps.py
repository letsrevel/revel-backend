import logging.handlers

import structlog
from django.apps import AppConfig


class CommonConfig(AppConfig):
    """Configuration for the common app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "common"
    queue_listener: logging.handlers.QueueListener | None = None

    def ready(self) -> None:
        """Initialize app-level services.

        Called once Django is fully loaded.
        """
        # Import and initialize observability
        from common.observability import init_profiling, init_tracing

        init_tracing()
        init_profiling()

        # Start QueueListener for async Loki logging
        self._start_queue_listener()

    def _start_queue_listener(self) -> None:
        """Start the QueueListener for async logging to Loki.

        This runs in a background thread and processes logs from the queue,
        sending them to Loki without blocking the main request threads.
        """
        import typing as t

        from django.conf import settings

        # Only start if observability is enabled
        if not getattr(settings, "ENABLE_OBSERVABILITY", False):
            return

        # Get handlers from logging config
        logging_config = t.cast(dict[str, t.Any], settings.LOGGING)
        handlers = logging_config.get("handlers", {})

        # Check if queue handler exists
        queue_handler_config = handlers.get("queue")
        loki_handler_config = handlers.get("loki")

        if not queue_handler_config or not loki_handler_config:
            return

        # Get the queue from the QueueHandler
        import logging

        root_logger = logging.getLogger()
        queue_handler = None

        for handler in root_logger.handlers:
            if isinstance(handler, logging.handlers.QueueHandler):
                queue_handler = handler
                break

        if not queue_handler:
            return

        # Create Loki handler instance for the listener
        from logging_loki import LokiHandler

        # Wrap Loki handler to handle connection errors gracefully
        class GracefulLokiHandler(LokiHandler):  # type: ignore[misc]
            """LokiHandler that silently fails on connection errors instead of crashing."""

            def handleError(self, record: logging.LogRecord) -> None:
                """Override to silently ignore connection errors to Loki.

                In CI/test environments, Loki may not be available.
                We don't want logging failures to crash the application.
                """
                # Silently ignore - don't call super() which would print to stderr
                pass

        loki_handler = GracefulLokiHandler(
            url=loki_handler_config["url"],
            tags=loki_handler_config["tags"],
            version=loki_handler_config["version"],
        )

        # Start QueueListener in background thread
        self.queue_listener = logging.handlers.QueueListener(
            queue_handler.queue,
            loki_handler,
            respect_handler_level=True,
        )
        self.queue_listener.start()

        logger = structlog.get_logger(__name__)
        logger.info(
            "Started QueueListener for async Loki logging",
            queue_maxsize=getattr(queue_handler.queue, "maxsize", None),
        )
