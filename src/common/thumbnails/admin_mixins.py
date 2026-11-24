"""Admin support for image fields with derived images (``THUMBNAIL_CONFIGS``)."""

import typing as t

from django.http import HttpRequest

from .config import THUMBNAIL_CONFIGS
from .files import stored_image_paths
from .service import replace_image_files_after_commit


class ImageSourceAdminMixin:
    """A change form that edits an image source keeps its derived images in step, as an API upload does.

    A new or removed image clears the derived images. After commit, the old files that no row names are
    deleted, and a new image gets its derived images. Put the mixin before ``ModelAdmin`` in the bases.
    """

    def save_model(self, request: HttpRequest, obj: t.Any, form: t.Any, change: bool) -> None:
        """Save, and replace the derived images of each image source that the form changed."""
        meta = obj._meta
        changed = [
            config
            for (app, model, field), config in THUMBNAIL_CONFIGS.items()
            if (app, model) == (meta.app_label, meta.model_name) and field in form.changed_data
        ]
        old = {config.source_field: stored_image_paths(type(obj), obj.pk, config.source_field) for config in changed}
        for config in changed:
            for spec in config.specs:
                setattr(obj, spec.field_name, None)
        super().save_model(request, obj, form, change)  # type: ignore[misc]
        for config in changed:
            replace_image_files_after_commit(obj, config.source_field, old[config.source_field] if change else [])
