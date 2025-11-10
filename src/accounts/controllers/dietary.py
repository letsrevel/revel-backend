"""Controllers for dietary restrictions and preferences management."""

import typing as t
from uuid import UUID

from django.db.models import Q, QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import ControllerBase, api_controller, route, status

from accounts import schema
from accounts.models import (
    DietaryPreference,
    DietaryRestriction,
    FoodItem,
    RevelUser,
    UserDietaryPreference,
)
from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from common.utils import get_or_create_with_race_protection


@api_controller("/dietary", tags=["Dietary"], auth=I18nJWTAuth(), throttle=UserDefaultThrottle())
class DietaryController(ControllerBase):
    """Controller for managing user dietary restrictions and preferences."""

    def user(self) -> RevelUser:
        """Get the authenticated user for this request."""
        return t.cast(RevelUser, self.context.request.user)  # type: ignore[union-attr]

    # Food Items Endpoints

    @route.get(
        "/food-items",
        response=list[schema.FoodItemSchema],
        url_name="list-food-items",
    )
    def list_food_items(self, search: str | None = None) -> QuerySet[FoodItem]:
        """Search for food items by name for autocomplete.

        Returns food items matching the search query (case-insensitive). Used for autocomplete
        when creating dietary restrictions. If no search query provided, returns recent items.
        """
        qs = FoodItem.objects.all()
        if search:
            qs = qs.filter(name__icontains=search)
        return qs[:20]  # Limit to 20 results for autocomplete

    @route.post(
        "/food-items",
        response={201: schema.FoodItemSchema, 200: schema.FoodItemSchema},
        url_name="create-food-item",
        throttle=WriteThrottle(),
    )
    def create_food_item(self, payload: schema.FoodItemCreateSchema) -> tuple[int, FoodItem]:
        """Create a new food item or return existing one (case-insensitive).

        Creates a food item if it doesn't exist. If a food item with the same name (case-insensitive)
        already exists, returns the existing item with 200 status. This prevents duplicate food items
        and allows users to freely create items during restriction creation.
        """
        existing = FoodItem.objects.filter(name__iexact=payload.name).first()
        if existing:
            return status.HTTP_200_OK, existing

        food_item = get_or_create_with_race_protection(
            FoodItem,
            Q(name__iexact=payload.name),
            {"name": payload.name},
        )
        return status.HTTP_201_CREATED, food_item

    # Dietary Restrictions Endpoints

    @route.get(
        "/restrictions",
        response=list[schema.DietaryRestrictionSchema],
        url_name="list-dietary-restrictions",
    )
    def list_dietary_restrictions(self) -> QuerySet[DietaryRestriction]:
        """List the authenticated user's dietary restrictions.

        Returns all dietary restrictions for the current user with food item details.
        """
        return DietaryRestriction.objects.filter(user=self.user()).select_related("food_item")

    @route.post(
        "/restrictions",
        response={201: schema.DietaryRestrictionSchema},
        url_name="create-dietary-restriction",
        throttle=WriteThrottle(),
    )
    def create_dietary_restriction(
        self, payload: schema.DietaryRestrictionCreateSchema
    ) -> tuple[int, DietaryRestriction]:
        """Create a new dietary restriction for the authenticated user.

        Creates a restriction linked to a food item. If the food item doesn't exist (case-insensitive),
        it will be created automatically. Returns 400 if a restriction for this food item already exists.
        """
        food_item = get_or_create_with_race_protection(
            FoodItem,
            Q(name__iexact=payload.food_item_name),
            {"name": payload.food_item_name},
        )

        if DietaryRestriction.objects.filter(user=self.user(), food_item=food_item).exists():
            raise HttpError(
                status.HTTP_400_BAD_REQUEST,
                str(_("You already have a restriction for this food item.")),
            )

        restriction = DietaryRestriction.objects.create(
            user=self.user(),
            food_item=food_item,
            restriction_type=payload.restriction_type,
            notes=payload.notes,
            is_public=payload.is_public,
        )
        return status.HTTP_201_CREATED, restriction

    @route.patch(
        "/restrictions/{restriction_id}",
        response=schema.DietaryRestrictionSchema,
        url_name="update-dietary-restriction",
        throttle=WriteThrottle(),
    )
    def update_dietary_restriction(
        self, restriction_id: UUID, payload: schema.DietaryRestrictionUpdateSchema
    ) -> DietaryRestriction:
        """Update an existing dietary restriction.

        Allows updating restriction type, notes, or visibility. Only the authenticated user
        can update their own restrictions. Returns 404 if restriction doesn't exist or doesn't
        belong to the user.
        """
        restriction = get_object_or_404(DietaryRestriction, id=restriction_id, user=self.user())

        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return restriction

        for field, value in update_data.items():
            setattr(restriction, field, value)

        restriction.save(update_fields=list(update_data.keys()))
        return restriction

    @route.delete(
        "/restrictions/{restriction_id}",
        response={204: None},
        url_name="delete-dietary-restriction",
        throttle=WriteThrottle(),
    )
    def delete_dietary_restriction(self, restriction_id: UUID) -> tuple[int, None]:
        """Delete a dietary restriction.

        Removes the restriction from the user's profile. Only the authenticated user can delete
        their own restrictions. Returns 404 if restriction doesn't exist or doesn't belong to the user.
        """
        restriction = get_object_or_404(DietaryRestriction, id=restriction_id, user=self.user())
        restriction.delete()
        return status.HTTP_204_NO_CONTENT, None

    # Dietary Preferences Endpoints

    @route.get(
        "/preferences",
        response=list[schema.DietaryPreferenceSchema],
        url_name="list-dietary-preferences",
    )
    def list_dietary_preferences(self) -> QuerySet[DietaryPreference]:
        """List all available dietary preferences (system-managed).

        Returns all predefined dietary preferences that users can select from.
        Users cannot create custom preferences, only select from this list.
        """
        return DietaryPreference.objects.all()

    @route.get(
        "/my-preferences",
        response=list[schema.UserDietaryPreferenceSchema],
        url_name="list-my-dietary-preferences",
    )
    def list_my_dietary_preferences(self) -> QuerySet[UserDietaryPreference]:
        """List the authenticated user's selected dietary preferences.

        Returns all dietary preferences the user has added to their profile with optional comments.
        """
        return UserDietaryPreference.objects.filter(user=self.user()).select_related("preference")

    @route.post(
        "/my-preferences",
        response={201: schema.UserDietaryPreferenceSchema},
        url_name="add-dietary-preference",
        throttle=WriteThrottle(),
    )
    def add_dietary_preference(
        self, payload: schema.UserDietaryPreferenceCreateSchema
    ) -> tuple[int, UserDietaryPreference]:
        """Add a dietary preference to the authenticated user's profile.

        Links a predefined dietary preference to the user with optional comment and visibility settings.
        Returns 400 if the preference is already added, 404 if the preference doesn't exist.
        """
        preference = get_object_or_404(DietaryPreference, id=payload.preference_id)

        if UserDietaryPreference.objects.filter(user=self.user(), preference=preference).exists():
            raise HttpError(
                status.HTTP_400_BAD_REQUEST,
                str(_("You have already added this dietary preference.")),
            )

        user_preference = UserDietaryPreference.objects.create(
            user=self.user(),
            preference=preference,
            comment=payload.comment,
            is_public=payload.is_public,
        )
        return status.HTTP_201_CREATED, user_preference

    @route.patch(
        "/my-preferences/{preference_id}",
        response=schema.UserDietaryPreferenceSchema,
        url_name="update-dietary-preference",
        throttle=WriteThrottle(),
    )
    def update_dietary_preference(
        self, preference_id: UUID, payload: schema.UserDietaryPreferenceUpdateSchema
    ) -> UserDietaryPreference:
        """Update a dietary preference comment or visibility.

        Allows updating the comment or visibility setting for a user's dietary preference.
        Returns 404 if the preference association doesn't exist or doesn't belong to the user.
        """
        user_preference = get_object_or_404(UserDietaryPreference, id=preference_id, user=self.user())

        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return user_preference

        for field, value in update_data.items():
            setattr(user_preference, field, value)

        user_preference.save(update_fields=list(update_data.keys()))
        return user_preference

    @route.delete(
        "/my-preferences/{preference_id}",
        response={204: None},
        url_name="delete-dietary-preference",
        throttle=WriteThrottle(),
    )
    def delete_dietary_preference(self, preference_id: UUID) -> tuple[int, None]:
        """Remove a dietary preference from the user's profile.

        Removes the preference association from the user's profile. Returns 404 if the preference
        association doesn't exist or doesn't belong to the user.
        """
        user_preference = get_object_or_404(UserDietaryPreference, id=preference_id, user=self.user())
        user_preference.delete()
        return status.HTTP_204_NO_CONTENT, None
