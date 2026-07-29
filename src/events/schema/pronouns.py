"""Pronoun distribution schemas for event attendees."""

from ninja import Schema
from pydantic import Field


class PronounCountSchema(Schema):
    """Count of attendees with a specific pronoun."""

    pronouns: str = Field(..., description="Pronoun string (e.g., 'he/him', 'she/her', 'they/them')")
    count: int = Field(..., description="Number of attendees with this pronoun")


class EventPronounDistributionSchema(Schema):
    """Pronoun distribution for event attendees.

    Every count is nullable: when the event hides attendee counts
    (``visibility_settings.show_attendee_count``) the distribution is served
    empty with null totals to non-privileged viewers. The per-pronoun counts sum
    to the attendee total, so redacting only the totals would not hide anything.
    """

    distribution: list[PronounCountSchema] = Field(
        default_factory=list,
        description="List of pronouns and their counts, ordered by count descending. Empty when counts are hidden.",
    )
    total_with_pronouns: int | None = Field(
        ..., description="Total attendees who have specified pronouns; null when counts are hidden"
    )
    total_without_pronouns: int | None = Field(
        ..., description="Total attendees without pronouns specified; null when counts are hidden"
    )
    total_attendees: int | None = Field(..., description="Total number of attendees; null when counts are hidden")
