# tests/performance/data/generators.py
"""Test data generators for performance tests.

Uses Faker for generating realistic test data.
"""

import random
from dataclasses import dataclass

from config import config
from faker import Faker


@dataclass
class TestUser:
    """Test user data."""

    email: str
    password: str
    first_name: str
    last_name: str


class TestDataGenerator:
    """Generator for test data.

    Provides methods for generating unique test data and
    selecting from pre-seeded users.
    """

    def __init__(self, seed: int | None = None) -> None:
        """Initialize generator.

        Args:
            seed: Random seed for reproducibility.
        """
        self.fake = Faker()
        if seed is not None:
            Faker.seed(seed)
            random.seed(seed)

        self._user_counter = 0

    def get_preseeded_user(self, index: int | None = None) -> TestUser:
        """Get a pre-seeded test user.

        Args:
            index: User index (0 to NUM_PRESEEDED_USERS-1).
                   If None, selects randomly.

        Returns:
            TestUser with pre-seeded credentials.
        """
        if index is None:
            index = random.randint(0, config.NUM_PRESEEDED_USERS - 1)

        return TestUser(
            email=config.get_user_email(index),
            password=config.DEFAULT_PASSWORD,
            first_name=f"PerfUser{index}",
            last_name="Test",
        )

    def get_random_preseeded_user(self) -> TestUser:
        """Get a random pre-seeded user.

        Returns:
            Randomly selected TestUser.
        """
        return self.get_preseeded_user()

    def generate_new_user(self) -> TestUser:
        """Generate a new unique test user.

        For use in registration tests.

        Returns:
            TestUser with unique generated email.
        """
        self._user_counter += 1
        unique_id = f"{self._user_counter}_{random.randint(1000, 9999)}"

        return TestUser(
            email=f"locust-new-{unique_id}@test.com",
            password=config.DEFAULT_PASSWORD,
            first_name=self.fake.first_name(),
            last_name=self.fake.last_name(),
        )

    def generate_guest_name(self) -> str:
        """Generate a guest name for tickets.

        Returns:
            Full name string.
        """
        return self.fake.name()

    def generate_pwyc_amount(self, min_price: float = 5.0, max_price: float = 50.0) -> str:
        """Generate a PWYC price amount.

        Args:
            min_price: Minimum price.
            max_price: Maximum price.

        Returns:
            Price as string with 2 decimal places.
        """
        price = random.uniform(min_price, max_price)
        return f"{price:.2f}"


# Singleton for convenience
_generator: TestDataGenerator | None = None


def get_data_generator(seed: int | None = None) -> TestDataGenerator:
    """Get or create TestDataGenerator singleton.

    Args:
        seed: Random seed (only used on first call).

    Returns:
        TestDataGenerator instance.
    """
    global _generator
    if _generator is None:
        _generator = TestDataGenerator(seed)
    return _generator
