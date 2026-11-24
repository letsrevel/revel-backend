"""Admin support for image fields with derived images (``THUMBNAIL_CONFIGS``)."""

import typing as t

from django.http import HttpRequest

from .config import THUMBNAIL_CONFIGS, ModelThumbnailConfig
from .service import replace_image_files_after_commit


class ImageSourceAdminMixin:
    """A change form that edits an image source keeps its derived images in step, as an API upload does.

    A new or removed image clears the derived images. After commit, the old files that no row names are
    deleted, and a new image gets its derived images. Put the mixin before ``ModelAdmin`` in the bases.
    """

    def save_model(self, request: HttpRequest, obj: t.Any, form: t.Any, change: bool) -> None:
        """Save, and replace the derived images of each image source that the form changed.

        On a change, the image fields are read under the row lock. The form object can be older than the row
        (a thumbnail task or an upload can have written since the page loaded), so the image fields that the
        form did not change get their stored values: the full save then writes them back unchanged.
        """
        meta = obj._meta
        configs = [
            config
            for (app, model, _field), config in THUMBNAIL_CONFIGS.items()
            if (app, model) == (meta.app_label, meta.model_name)
        ]
        changed = [config for config in configs if config.source_field in form.changed_data]
        old: dict[str, list[str]] = {}
        if change and configs:
            names = {config.source_field: _image_fields(config) for config in configs}
            flat = [name for fields in names.values() for name in fields]
            # NO KEY: other transactions' foreign-key checks (KEY SHARE) must not wait.
            rows = type(obj)._default_manager.select_for_update(no_key=True).filter(pk=obj.pk)
            row = rows.values_list(*flat).first()
            stored = dict(zip(flat, row or (), strict=False))
            for config in configs:
                fields = names[config.source_field]
                if config in changed:
                    old[config.source_field] = [stored[name] for name in fields if stored.get(name)]
                else:
                    for name in fields:
                        setattr(obj, name, stored.get(name))
        for config in changed:
            for spec in config.specs:
                setattr(obj, spec.field_name, None)
        super().save_model(request, obj, form, change)  # type: ignore[misc]
        for config in changed:
            replace_image_files_after_commit(obj, config.source_field, old.get(config.source_field, []))


def _image_fields(config: ModelThumbnailConfig) -> list[str]:
    return [config.source_field, *(spec.field_name for spec in config.specs)]
