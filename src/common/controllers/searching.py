import typing as t

from django.db.models import QuerySet
from ninja_extra.searching import Searching


class DistinctSearching(Searching):
    """`Searching` that de-duplicates results after filtering.

    The default `Searching` builds an OR across `search_fields` and applies it as a single
    `queryset.filter(...)`. When any search field spans a multiplying relation (a many-to-many
    or reverse foreign key, e.g. `tags__tag__name`), the join emits one row per related match,
    so an object appears once per matching related row. Appending `.distinct()` collapses those
    duplicates back to one row per object.

    Use this instead of `Searching` whenever `search_fields` traverses an M2M/reverse relation;
    forward FK joins cannot multiply and do not need it.
    """

    def searching_queryset(
        self, items: t.Union[QuerySet[t.Any], t.List[t.Any]], searching_input: "Searching.Input"
    ) -> t.Union[QuerySet[t.Any], t.List[t.Any]]:
        """Apply the base search filter, then de-duplicate querysets with `.distinct()`."""
        result = super().searching_queryset(items, searching_input)
        if isinstance(result, QuerySet):
            return result.distinct()
        return result
