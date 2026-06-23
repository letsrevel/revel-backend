"""Tag models and the taggable mixin (generic, content-type based tagging)."""

import typing as t

from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models

from common.models.base import TimeStampedModel


class Tag(TimeStampedModel):
    name = models.CharField(max_length=64, unique=True, db_index=True)
    description = models.TextField(blank=True, null=True)
    color = models.CharField(max_length=7, blank=True, null=True, help_text="Hex color (e.g. #FF0099)")
    icon = models.CharField(max_length=64, blank=True, null=True, help_text="Optional icon name")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")

    @staticmethod
    def normalize_name(name: str) -> str:
        """Strip surrounding whitespace and leading ``#`` (hashtag) characters."""
        return name.strip().lstrip("#").strip()

    def clean(self) -> None:
        """Normalize the name before validation."""
        if self.name:
            self.name = self.normalize_name(self.name)

    def __str__(self) -> str:
        return self.name


class TagAssignment(TimeStampedModel):
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="assignments")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    content_object = GenericForeignKey("content_type", "object_id")
    assigned_by = models.UUIDField(null=True, blank=True)  # Could link to user, if wanted

    class Meta:
        unique_together = ("tag", "content_type", "object_id")
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.tag} -> {self.content_object}"


class TagManager:
    def __init__(self, instance: models.Model):
        """Init."""
        self.instance = instance

    def all(self) -> t.List[Tag]:
        """Return all tags."""
        return [ta.tag for ta in self.instance.tags.all()]  # type: ignore[attr-defined]

    def add(self, *names: str) -> None:
        """Add the tags."""
        from django.contrib.contenttypes.models import ContentType  # local import to avoid circularity
        from django.db.models import Q

        from common.utils import get_or_create_with_race_protection

        ct = ContentType.objects.get_for_model(self.instance.__class__)
        for name in names:
            name = Tag.normalize_name(name)
            if not name:
                continue  # skip blanks
            tag, _ = get_or_create_with_race_protection(Tag, Q(name=name), {"name": name})
            TagAssignment.objects.get_or_create(
                tag=tag,
                content_type=ct,
                object_id=self.instance.pk,
            )

    def remove(self, *names: str) -> None:
        """Remove the tags."""
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(self.instance.__class__)
        TagAssignment.objects.filter(
            tag__name__in=names,
            content_type=ct,
            object_id=self.instance.pk,
        ).delete()

    def clear(self) -> None:
        """Delete all tags."""
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(self.instance.__class__)
        TagAssignment.objects.filter(
            content_type=ct,
            object_id=self.instance.pk,
        ).delete()


class TaggableMixin(models.Model):
    tags = GenericRelation(
        TagAssignment,
        related_query_name="%(class)s",
        content_type_field="content_type",
        object_id_field="object_id",
    )

    class Meta:
        abstract = True

    @property
    def tags_manager(self) -> TagManager:
        """Helper to get the tag manager."""
        return TagManager(self)

    def add_tags(self, *names: str) -> None:
        """Add tags."""
        self.tags_manager.add(*names)

    def remove_tags(self, *names: str) -> None:
        """Remove tags."""
        self.tags_manager.remove(*names)

    def clear_tags(self) -> None:
        """Clear tags."""
        self.tags_manager.clear()

    def get_tags(self) -> t.List[Tag]:
        """Get tags."""
        return self.tags_manager.all()
