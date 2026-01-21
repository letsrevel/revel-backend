"""User seeding module."""

from accounts.models import (
    DietaryPreference,
    DietaryRestriction,
    FoodItem,
    RevelUser,
    UserDietaryPreference,
)
from events.management.commands.seeder.base import BaseSeeder

# Common food items for restrictions
FOOD_ITEMS = [
    "Peanuts",
    "Tree Nuts",
    "Milk",
    "Eggs",
    "Wheat",
    "Soy",
    "Fish",
    "Shellfish",
    "Sesame",
    "Gluten",
    "Lactose",
    "Corn",
    "Mustard",
    "Celery",
    "Lupin",
    "Mollusks",
    "Sulfites",
    "Red Meat",
    "Poultry",
    "Alcohol",
]

# Common dietary preferences
DIETARY_PREFERENCES = [
    "Vegetarian",
    "Vegan",
    "Pescatarian",
    "Halal",
    "Kosher",
    "Gluten-Free",
    "Dairy-Free",
    "Keto",
    "Paleo",
    "Low-FODMAP",
]

# Common pronouns
PRONOUNS = [
    "he/him",
    "she/her",
    "they/them",
    "he/they",
    "she/they",
    "any pronouns",
    "",  # Some users don't specify
]


class UserSeeder(BaseSeeder):
    """Seeder for RevelUser and dietary-related models."""

    def seed(self) -> None:
        """Seed users and dietary data."""
        self._create_hashed_password()
        self._create_users()
        self._create_dietary_data()

    def _create_hashed_password(self) -> None:
        """Pre-hash password once for all users."""
        self.log("Pre-hashing password...")
        temp_user = RevelUser()
        temp_user.set_password("password")
        self.state.hashed_password = temp_user.password

    def _create_users(self) -> None:
        """Create users with role distribution."""
        self.log(f"Creating {self.config.num_users} users...")

        users_to_create: list[RevelUser] = []

        # Calculate distribution
        num_owners = self.config.num_organizations
        min_staff, max_staff = self.config.staff_per_org
        avg_staff = (min_staff + max_staff) // 2
        num_staff = num_owners * avg_staff

        remaining = self.config.num_users - num_owners - num_staff
        num_members = int(remaining * 0.6)
        num_regular = remaining - num_members

        self.log(f"  Owners: {num_owners}")
        self.log(f"  Staff: {num_staff}")
        self.log(f"  Members: {num_members}")
        self.log(f"  Regular: {num_regular}")

        # Create owner users
        for i in range(num_owners):
            users_to_create.append(self._make_user(f"owner_{i}", role="owner"))

        # Create staff users
        for i in range(num_staff):
            users_to_create.append(self._make_user(f"staff_{i}", role="staff"))

        # Create member users
        for i in range(num_members):
            users_to_create.append(self._make_user(f"member_{i}", role="member"))

        # Create regular users
        for i in range(num_regular):
            users_to_create.append(self._make_user(f"user_{i}", role="regular"))

        # Batch create all users
        created = self.batch_create(RevelUser, users_to_create, desc="Creating users")

        # Categorize users by role
        idx = 0
        self.state.owner_users = created[idx : idx + num_owners]
        idx += num_owners

        self.state.staff_users = created[idx : idx + num_staff]
        idx += num_staff

        self.state.member_users = created[idx : idx + num_members]
        idx += num_members

        self.state.regular_users = created[idx:]
        self.state.users = created

        self.log(f"  Created {len(created)} users")

    def _make_user(self, username_base: str, role: str = "regular") -> RevelUser:
        """Create a single user instance (not saved yet)."""
        first_name = self.faker.first_name()
        last_name = self.faker.last_name()

        # Role-specific email verification rates
        if role == "owner":
            email_verified = True
        elif role == "staff":
            email_verified = self.random_bool(0.95)
        elif role == "member":
            email_verified = self.random_bool(0.8)
        else:
            email_verified = self.random_bool(0.6)

        return RevelUser(
            username=f"{username_base}@seed.test",
            email=f"{username_base}@seed.test",
            password=self.state.hashed_password,
            first_name=first_name,
            last_name=last_name,
            preferred_name=f"{first_name} {last_name}" if self.random_bool(0.7) else "",
            pronouns=self.random_choice(PRONOUNS),
            email_verified=email_verified,
            is_active=True,
            guest=False,
        )

    def _create_dietary_data(self) -> None:
        """Create food items, dietary preferences, restrictions, and user preferences."""
        self.log("Creating dietary data...")

        # Create food items (check for existing first - case insensitive constraint)
        existing_food_names = {name.lower() for name in FoodItem.objects.values_list("name", flat=True)}
        food_items_to_create = [FoodItem(name=name) for name in FOOD_ITEMS if name.lower() not in existing_food_names]
        if food_items_to_create:
            self.batch_create(
                FoodItem,
                food_items_to_create,
                desc="Creating food items",
            )
        # Get all food items for linking (including pre-existing)
        self.state.food_items = list(FoodItem.objects.all())
        self.log(f"  Food items available: {len(self.state.food_items)}")

        # Create dietary preferences (system-managed)
        # First check if they exist (they might be seeded via bootstrap_helpers)
        existing_prefs = set(DietaryPreference.objects.values_list("name", flat=True))
        prefs_to_create = [DietaryPreference(name=name) for name in DIETARY_PREFERENCES if name not in existing_prefs]
        if prefs_to_create:
            self.batch_create(
                DietaryPreference,
                prefs_to_create,
                desc="Creating dietary preferences",
            )

        # Get all preferences for linking
        all_prefs = list(DietaryPreference.objects.all())

        # Create dietary restrictions for ~30% of users
        self.log("Creating dietary restrictions...")
        restrictions_to_create: list[DietaryRestriction] = []
        users_with_restrictions = self.random_subset(
            self.state.users,
            int(len(self.state.users) * 0.2),
            int(len(self.state.users) * 0.4),
        )

        for user in users_with_restrictions:
            # Each user gets 1-3 restrictions
            num_restrictions = self.random_int(1, 3)
            user_foods = self.random_sample(self.state.food_items, num_restrictions)

            for food in user_foods:
                restrictions_to_create.append(
                    DietaryRestriction(
                        user=user,
                        food_item=food,
                        restriction_type=self.random_choice(list(DietaryRestriction.RestrictionType.values)),
                        notes=self.faker.sentence() if self.random_bool(0.2) else "",
                        is_public=self.random_bool(0.5),
                    )
                )

        self.batch_create(
            DietaryRestriction,
            restrictions_to_create,
            desc="Creating dietary restrictions",
        )
        self.log(f"  Created {len(restrictions_to_create)} dietary restrictions")

        # Create user dietary preferences for ~40% of users
        self.log("Creating user dietary preferences...")
        user_prefs_to_create: list[UserDietaryPreference] = []
        users_with_prefs = self.random_subset(
            self.state.users,
            int(len(self.state.users) * 0.3),
            int(len(self.state.users) * 0.5),
        )

        for user in users_with_prefs:
            # Each user gets 1-2 preferences
            num_prefs = self.random_int(1, 2)
            user_pref_choices = self.random_sample(all_prefs, num_prefs)

            for pref in user_pref_choices:
                user_prefs_to_create.append(
                    UserDietaryPreference(
                        user=user,
                        preference=pref,
                        comment=self.faker.sentence() if self.random_bool(0.1) else "",
                        is_public=self.random_bool(0.5),
                    )
                )

        self.batch_create(
            UserDietaryPreference,
            user_prefs_to_create,
            desc="Creating user dietary preferences",
        )
        self.log(f"  Created {len(user_prefs_to_create)} user dietary preferences")
