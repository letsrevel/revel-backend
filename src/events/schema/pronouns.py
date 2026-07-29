"""Pronoun distribution schemas for event attendees."""

from ninja import Schema
from pydantic import Field


class PronounCountSchema(Schema):
    """Count of attendees with a specific pronoun."""

    pronouns: str = Field(..., description="Pronoun string (e.g., 'he/him', 'she/her', 'they/them')")
    count: int = Field(..., description="Number of attendees with this pronoun")


class EventPronounDistributionSchema(Schema):
    """Pronoun distribution for event attendees.

    Every count is nullable: to non-privileged viewers, the distribution is
    served empty with null totals when the event has not opted into publishing
    it (``visibility_settings.show_pronoun_distribution``) or when it hides
    attendee counts (``visibility_settings.show_attendee_count``). The
    per-pronoun counts sum to the attendee total, so redacting only the totals
    would not hide anything.
    """

    distribution: list[PronounCountSchema] = Field(
        default_factory=list,
        description=(
            "List of pronouns and their counts, ordered by count descending. Empty when the "
            "distribution is not published or counts are hidden."
        ),
    )
    total_with_pronouns: int | None = Field(
        ...,
        description=(
            "Total attendees who have specified pronouns; null when the distribution is not "
            "published or counts are hidden"
        ),
    )
    total_without_pronouns: int | None = Field(
        ...,
        description=(
            "Total attendees without pronouns specified; null when the distribution is not "
            "published or counts are hidden"
        ),
    )
    total_attendees: int | None = Field(
        ...,
        description=("Total number of attendees; null when the distribution is not published or counts are hidden"),
    )
