"""Dietary summary schemas for event attendees."""

from ninja import Schema
from pydantic import Field

from accounts.models import DietaryRestriction


class AggregatedDietaryRestrictionSchema(Schema):
    """Aggregated dietary restriction data for event attendees."""

    food_item: str = Field(..., description="Food or ingredient name")
    severity: DietaryRestriction.RestrictionType = Field(
        ..., description="Restriction severity (dislike, intolerant, allergy, severe_allergy)"
    )
    attendee_count: int = Field(..., description="Number of attendees with this restriction")
    notes: list[str] = Field(default_factory=list, description="Non-empty notes from attendees")


class AggregatedDietaryPreferenceSchema(Schema):
    """Aggregated dietary preference data for event attendees."""

    name: str = Field(..., description="Dietary preference name")
    attendee_count: int = Field(..., description="Number of attendees with this preference")
    comments: list[str] = Field(default_factory=list, description="Non-empty comments from attendees")


class EventDietarySummarySchema(Schema):
    """Aggregated dietary information for event attendees."""

    restrictions: list[AggregatedDietaryRestrictionSchema] = Field(
        default_factory=list,
        description="Aggregated dietary restrictions",
    )
    preferences: list[AggregatedDietaryPreferenceSchema] = Field(
        default_factory=list,
        description="Aggregated dietary preferences",
    )
