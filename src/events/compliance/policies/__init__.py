"""One module per country; each registers itself with ``@register("XX")``.

Every module in this package is imported on startup, so adding a country is adding a
file here — no list to update.
"""

import importlib
import pkgutil

for _module in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_module.name}")
