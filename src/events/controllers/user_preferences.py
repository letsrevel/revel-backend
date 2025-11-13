"""Controller for managing user preferences."""

from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from events import models, schema
from events.service.user_preferences_service import set_general_preferences


@api_controller("/preferences", auth=I18nJWTAuth(), tags=["User Preferences"])
class UserPreferencesController(UserAwareController):
    """Controller for managing user preferences."""

    @route.get("/general", url_name="get_general_preferences", response=schema.GeneralUserPreferencesSchema)
    def get_general_preferences(self) -> models.GeneralUserPreferences:
        """Get your global preferences.

        Returns your visibility and location settings.
        For notification preferences, use the /notification-preferences endpoint.
        """
        return self.user().general_preferences

    @route.put("/general", url_name="update_general_preferences", response=schema.GeneralUserPreferencesSchema)
    def update_general_preferences(
        self, payload: schema.GeneralUserPreferencesUpdateSchema
    ) -> models.GeneralUserPreferences:
        """Update your global preferences.

        Modify visibility and location settings.
        For notification preferences, use the /notification-preferences endpoint.
        """
        return set_general_preferences(self.user().general_preferences, payload)
