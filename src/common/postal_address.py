"""Structured postal address value type (EN 16931 / BT-50 to BT-55 shape).

Pure Python on purpose: no Django imports, so it can be used by models, schemas and
invoice rendering alike.
"""

import dataclasses
import typing as t

_REQUIRED_FIELDS: t.Final[tuple[str, ...]] = ("line1", "postal_code", "city", "country_code")


@dataclasses.dataclass(frozen=True)
class PostalAddress:
    """An immutable postal address. Every field is optional; blanks are empty strings."""

    line1: str = ""
    line2: str = ""
    postal_code: str = ""
    city: str = ""
    subdivision: str = ""
    country_code: str = ""

    # Field name -> column-name suffix; a model prefix (``billing_``, ``seller_``, ``buyer_``) is prepended.
    FIELD_SUFFIXES: t.ClassVar[dict[str, str]] = {
        "line1": "address_line1",
        "line2": "address_line2",
        "postal_code": "postal_code",
        "city": "city",
        "subdivision": "subdivision",
        "country_code": "country_code",
    }

    def to_fields(self, prefix: str) -> dict[str, str]:
        """Map the address to ``{prefix + suffix: value}`` model/schema attribute names."""
        return {f"{prefix}{suffix}": getattr(self, name) for name, suffix in self.FIELD_SUFFIXES.items()}

    @classmethod
    def from_fields(cls, obj: object, prefix: str) -> "PostalAddress":
        """Read an address from ``obj``'s ``prefix + suffix`` attributes."""
        return cls(**{name: getattr(obj, f"{prefix}{suffix}") for name, suffix in cls.FIELD_SUFFIXES.items()})

    def missing_fields(self) -> list[str]:
        """Return the required fields that are blank, in declaration order.

        Returns:
            Names among ``line1``, ``postal_code``, ``city`` and ``country_code`` that are blank.
        """
        return [name for name in _REQUIRED_FIELDS if not getattr(self, name)]

    @property
    def is_complete(self) -> bool:
        """Whether every required field is filled in."""
        return not self.missing_fields()

    @property
    def is_empty(self) -> bool:
        """Whether every field is blank."""
        return not any(dataclasses.astuple(self))

    def display_lines(self) -> list[str]:
        """Return the address as printable lines, skipping blank ones.

        Returns:
            Non-empty lines: line1, line2, "postal_code city", subdivision, country_code.
        """
        lines = [self.line1, self.line2, f"{self.postal_code} {self.city}".strip(), self.subdivision, self.country_code]
        return [line for line in lines if line]
