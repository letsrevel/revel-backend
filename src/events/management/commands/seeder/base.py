"""Base seeder class with shared utilities."""

import gc
import random
import typing as t
from collections.abc import Sequence

from django.db import models
from faker import Faker
from tqdm import tqdm

from events.management.commands.seeder.config import SeederConfig
from events.management.commands.seeder.state import SeederState

ModelType = t.TypeVar("ModelType", bound=models.Model)


class BaseSeeder:
    """Base class for all seeders with common utilities."""

    def __init__(
        self,
        config: SeederConfig,
        state: SeederState,
        stdout: t.Any,
    ) -> None:
        """Initialize the seeder with config, state, and output stream."""
        self.config = config
        self.state = state
        self.stdout = stdout
        self.rand = random.Random(config.seed)
        self.faker = Faker()
        self.faker.seed_instance(config.seed)

    def log(self, message: str) -> None:
        """Log a message to stdout."""
        self.stdout.write(message)

    def batch_create(
        self,
        model: type[ModelType],
        objects: Sequence[ModelType],
        batch_size: int = 500,
        desc: str | None = None,
        ignore_conflicts: bool = False,
    ) -> list[ModelType]:
        """Bulk create objects in batches with progress bar.

        Args:
            model: The Django model class
            objects: List of model instances to create
            batch_size: Number of objects per batch
            desc: Description for progress bar
            ignore_conflicts: If True, ignore unique constraint violations

        Returns:
            List of created objects with IDs populated
            (note: IDs may not be populated when ignore_conflicts=True)
        """
        if not objects:
            return []

        created: list[ModelType] = []
        description = desc or f"Creating {model.__name__}s"

        for i in tqdm(
            range(0, len(objects), batch_size),
            desc=description,
            total=(len(objects) + batch_size - 1) // batch_size,
        ):
            batch = objects[i : i + batch_size]
            created.extend(
                model.objects.bulk_create(batch, batch_size, ignore_conflicts=ignore_conflicts)  # type: ignore[attr-defined]
            )
            gc.collect()

        return created

    def weighted_choice(self, weights: dict[str, float]) -> str:
        """Pick a key from a weighted distribution.

        Args:
            weights: Dict mapping choices to their probabilities

        Returns:
            One of the keys, selected by weighted random
        """
        choices = list(weights.keys())
        probs = list(weights.values())
        return self.rand.choices(choices, weights=probs, k=1)[0]

    def random_subset(
        self,
        items: Sequence[t.Any],
        min_count: int,
        max_count: int,
    ) -> list[t.Any]:
        """Get a random subset of items.

        Args:
            items: Sequence to sample from
            min_count: Minimum number of items
            max_count: Maximum number of items

        Returns:
            Random subset of items
        """
        if not items:
            return []

        count = self.rand.randint(min_count, min(max_count, len(items)))
        return self.rand.sample(list(items), count)

    def random_bool(self, probability: float = 0.5) -> bool:
        """Return True with given probability."""
        return self.rand.random() < probability

    def random_int(self, min_val: int, max_val: int) -> int:
        """Return random integer in range [min_val, max_val]."""
        return self.rand.randint(min_val, max_val)

    def random_choice(self, items: Sequence[t.Any]) -> t.Any:
        """Return random item from sequence."""
        return self.rand.choice(items)

    def random_sample(self, items: Sequence[t.Any], k: int) -> list[t.Any]:
        """Return k random items from sequence without replacement."""
        return self.rand.sample(list(items), min(k, len(items)))

    def seed(self) -> None:
        """Override in subclasses to perform seeding."""
        raise NotImplementedError
