"""France (#1061). CGI art. 290 quater ticket mentions.

Layer 1 needs no restriction: the mentions a ticket must carry (organizer, price paid or
"free", a system-assigned sequential number) are part of the EU common set every policy
prints. Later layers (operation journal, SIBIL/bordereau exports) hook in here.

Docs: https://docs.letsrevel.io/compliance/eu/fr/ (docs/compliance/eu/fr.md).
"""

from events.compliance.base import DefaultEUPolicy
from events.compliance.registry import register


@register("FR")
class FrancePolicy(DefaultEUPolicy):
    """France: the EU defaults (see the module docstring)."""
