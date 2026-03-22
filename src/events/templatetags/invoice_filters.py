"""Template filters for invoice rendering.

Delegates to ``common.templatetags.invoice_filters`` — the canonical location.
This shim exists so existing templates using ``{% load invoice_filters %}``
from the events app continue to work.

Note: Django's template loader resolves ``invoice_filters`` from common first
(alphabetical order), so this module is only loaded when explicitly referenced
via ``events.templatetags.invoice_filters``.
"""

# Re-export so ``from events.templatetags.invoice_filters import register``
# still works for any code that imports it directly.
from common.templatetags.invoice_filters import register as register  # noqa: F401
