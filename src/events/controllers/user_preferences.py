from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route
from ninja_jwt.authentication import JWTAuth

from events import models, schema
from events.service.user_preferences_service import set_preferences

from .user_aware_controller import UserAwareController


@api_controller("/preferences", auth=JWTAuth(), tags=["User Preferences"])
class UserPreferencesController(UserAwareController):
    """Controller for managing user preferences at different levels."""

    @route.get("/general", url_name="get_general_preferences", response=schema.GeneralUserPreferencesSchema)
    def get_general_preferences(self) -> models.GeneralUserPreferences:
        """Retrieve the general preferences for the authenticated user."""
        return self.user().general_preferences

    @route.put("/general", url_name="update_general_preferences", response=schema.GeneralUserPreferencesSchema)
    def update_global_preferences(
        self, payload: schema.GeneralUserPreferencesUpdateSchema, overwrite_children: bool = False
    ) -> models.GeneralUserPreferences:
        """Update the general preferences for the authenticated user."""
        return set_preferences(self.user().general_preferences, payload, overwrite_children=overwrite_children)

    @route.get(
        "/organization/{organization_id}",
        url_name="get_organization_preferences",
        response=schema.UserOrganizationPreferencesSchema,
    )
    def get_organization_preferences(self, organization_id: UUID) -> models.UserOrganizationPreferences:
        """Retrieve organization-specific preferences for the authenticated user."""
        organization = get_object_or_404(models.Organization.objects.for_user(self.user()), id=organization_id)
        return get_object_or_404(models.UserOrganizationPreferences, organization=organization, user=self.user())

    @route.put(
        "/organization/{organization_id}",
        url_name="update_organization_preferences",
        response=schema.UserOrganizationPreferencesSchema,
    )
    def update_organization_preferences(
        self,
        organization_id: UUID,
        payload: schema.UserOrganizationPreferencesUpdateSchema,
        overwrite_children: bool = False,
    ) -> models.UserOrganizationPreferences:
        """Update organization-specific preferences for the authenticated user."""
        org = get_object_or_404(models.Organization.objects.for_user(self.user()), id=organization_id)
        org_pref, _ = models.UserOrganizationPreferences.objects.get_or_create(
            user=self.user(), organization=org, defaults=payload.model_dump(exclude_unset=True)
        )
        return set_preferences(org_pref, payload, overwrite_children=overwrite_children)

    @route.get(
        "/event-series/{series_id}",
        url_name="get_event_series_preferences",
        response=schema.UserEventSeriesPreferencesSchema,
    )
    def get_event_series_preferences(self, series_id: UUID) -> models.UserEventSeriesPreferences:
        """Retrieve event series-specific preferences for the authenticated user."""
        evt_series = get_object_or_404(models.EventSeries.objects.for_user(self.user()), id=series_id)
        return get_object_or_404(models.UserEventSeriesPreferences, event_series=evt_series, user=self.user())

    @route.put(
        "/event-series/{series_id}",
        url_name="update_event_series_preferences",
        response=schema.UserEventSeriesPreferencesSchema,
    )
    def update_event_series_preferences(
        self, series_id: UUID, payload: schema.UserEventSeriesPreferencesUpdateSchema, overwrite_children: bool = False
    ) -> models.UserEventSeriesPreferences:
        """Update event series-specific preferences for the authenticated user."""
        event_series = get_object_or_404(models.EventSeries.objects.for_user(self.user()), id=series_id)
        series_pref, _ = models.UserEventSeriesPreferences.objects.get_or_create(
            event_series=event_series, user=self.user(), defaults=payload.model_dump()
        )
        return set_preferences(series_pref, payload, overwrite_children=overwrite_children)

    @route.get("/event/{event_id}", url_name="get_event_preferences", response=schema.UserEventPreferencesSchema)
    def get_event_preferences(self, event_id: UUID) -> models.UserEventPreferences:
        """Retrieve event-specific preferences for the authenticated user."""
        event = get_object_or_404(models.Event.objects.for_user(self.user(), include_past=True), id=event_id)
        return get_object_or_404(models.UserEventPreferences, event=event, user=self.user())

    @route.put("/event/{event_id}", url_name="update_event_preferences", response=schema.UserEventPreferencesSchema)
    def update_event_preferences(
        self, event_id: UUID, payload: schema.UserEventPreferencesUpdateSchema, overwrite_children: bool = False
    ) -> models.UserEventPreferences:
        """Update event-specific preferences for the authenticated user."""
        event = get_object_or_404(models.Event.objects.for_user(self.user(), include_past=True), id=event_id)
        event_pref, _ = models.UserEventPreferences.objects.get_or_create(
            event=event, user=self.user(), defaults=payload.model_dump()
        )
        return set_preferences(event_pref, payload, overwrite_children=overwrite_children)
