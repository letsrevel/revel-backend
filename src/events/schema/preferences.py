"""User preferences schemas."""

from ninja import Schema

from events import models
from geo.schema import CitySchema

from .mixins import CityBaseMixin

DEFAULT_VISIBILITY_PREFERENCE = models.BaseUserPreferences.VisibilityPreference.NEVER


class GeneralUserPreferencesSchema(Schema):
    """Schema for general user preferences (visibility and location)."""

    show_me_on_attendee_list: models.BaseUserPreferences.VisibilityPreference = DEFAULT_VISIBILITY_PREFERENCE
    city: CitySchema | None = None


class GeneralUserPreferencesUpdateSchema(CityBaseMixin):
    """Schema for updating general user preferences."""

    show_me_on_attendee_list: models.BaseUserPreferences.VisibilityPreference = DEFAULT_VISIBILITY_PREFERENCE
