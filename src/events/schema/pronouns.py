"""Pronoun distribution schemas for event attendees."""

from ninja import Schema
from pydantic import Field


class PronounCountSchema(Schema):
    """Count of attendees with a specific pronoun."""

    pronouns: str = Field(..., description="Pronoun string (e.g., 'he/him', 'she/her', 'they/them')")
    count: int = Field(..., description="Number of attendees with this pronoun")


class EventPronounDistributionSchema(Schema):
    """Pronoun distribution for event attendees."""

    distribution: list[PronounCountSchema] = Field(
        default_factory=list,
        description="List of pronouns and their counts, ordered by count descending",
    )
    total_with_pronouns: int = Field(..., description="Total attendees who have specified pronouns")
    total_without_pronouns: int = Field(..., description="Total attendees without pronouns specified")
    total_attendees: int = Field(..., description="Total number of attendees")
