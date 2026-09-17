"""Developer portal (spec §8.3): a user's own manually registered OAuth clients.

``requires_verified_email=True`` on the whole controller: registering a client that other people
will be asked to trust is the higher-trust half of this feature, while *seeing and cutting off*
your own grants is not — that lives on plain JWT auth in ``connections.py`` (R-73).

There is deliberately no logo-upload route yet. R-35 requires it to go through
``common.service.upload_service.safe_save_uploaded_file`` (the only dispatch site for
``THUMBNAIL_CONFIGS``, so the only way ``logo_thumbnail`` is ever generated), and that service
records every upload in ``FileUploadAudit``, whose ``instance_pk`` is a ``UUIDField``. This
model's primary key is DOT's ``BigAutoField``, so the audit insert raises and the upload 500s.
Fixing it means either widening that shared audit column (an ``ALTER TYPE`` table rewrite on a
production audit table) or giving the swapped application model a UUID primary key — both
cross-cutting calls that belong to whoever owns the branch, not to this route. Until then an
operator sets the logo in the admin and ``manage.py generate_thumbnails`` builds the thumbnail,
which is R-35's other documented dispatch site.
"""

import typing as t

from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import UserDefaultThrottle, WriteThrottle
from oauth import schema
from oauth.models import OAuthApplication
from oauth.service import app_service


@api_controller(
    "/oauth/apps",
    auth=I18nJWTAuth(requires_verified_email=True),
    tags=["OAuth - Developer Apps"],
    throttle=UserDefaultThrottle(),
)
class OAuthAppController(UserAwareController):
    def get_queryset(self) -> QuerySet[OAuthApplication]:
        """The caller's own manual apps, annotated with their live connection count.

        Ownership is a filter rather than a check, so every route below is scoped to the caller
        by construction and a foreign app is a 404 instead of a 403. Only *live* tokens count:
        expired rows linger until ``cleartokens`` runs, and counting them would overstate an
        app's reach by up to a day, disagreeing with the Connected Apps screen (spec §8.4).

        Returns:
            The queryset every route resolves against.
        """
        return OAuthApplication.objects.filter(
            user=self.user(), registration_source=OAuthApplication.RegistrationSource.MANUAL
        ).annotate(
            connections_count=Count(
                "accesstoken__user", distinct=True, filter=Q(accesstoken__expires__gt=timezone.now())
            )
        )

    def get_one(self, app_id: int) -> OAuthApplication:
        """One of the caller's apps, or a 404.

        Args:
            app_id: The app's primary key.

        Returns:
            The app.
        """
        return t.cast(OAuthApplication, self.get_object_or_exception(self.get_queryset(), pk=app_id))

    @route.get("/", url_name="oauth_apps_list", response=list[schema.OAuthAppSchema])
    def list_apps(self) -> QuerySet[OAuthApplication]:
        """List the apps you have registered, newest first."""
        return self.get_queryset().order_by("-created")

    @route.post(
        "/", url_name="oauth_apps_create", response={201: schema.OAuthAppCreatedSchema}, throttle=WriteThrottle()
    )
    def create_app(self, payload: schema.OAuthAppCreatePayload) -> tuple[int, OAuthApplication]:
        """Register an app. A confidential client's secret is returned here and never again."""
        app, secret = app_service.create_app(self.user(), payload)
        app.plaintext_client_secret = secret
        return 201, app

    @route.get("/{app_id}", url_name="oauth_apps_get", response=schema.OAuthAppSchema)
    def get_app(self, app_id: int) -> OAuthApplication:
        """Retrieve one of your apps."""
        return self.get_one(app_id)

    @route.patch("/{app_id}", url_name="oauth_apps_update", response=schema.OAuthAppSchema, throttle=WriteThrottle())
    def update_app(self, app_id: int, payload: schema.OAuthAppUpdatePayload) -> OAuthApplication:
        """Update one of your apps; removing a scope revokes the tokens that carry it."""
        data = payload.model_dump(exclude_unset=True)
        app = self.get_one(app_id)
        return app_service.update_app(app, data) if data else app

    @route.delete("/{app_id}", url_name="oauth_apps_delete", response={204: None}, throttle=WriteThrottle())
    def delete_app(self, app_id: int) -> tuple[int, None]:
        """Delete one of your apps, along with every token it was issued."""
        app_service.delete_app(self.get_one(app_id))
        return 204, None

    @route.post(
        "/{app_id}/rotate-secret",
        url_name="oauth_apps_rotate_secret",
        response=schema.OAuthAppCreatedSchema,
        throttle=WriteThrottle(),
    )
    def rotate_secret(self, app_id: int) -> OAuthApplication:
        """Issue a new client secret. Existing tokens keep working; the old secret stops."""
        app = self.get_one(app_id)
        app.plaintext_client_secret = app_service.rotate_secret(app)
        return app

    @route.post(
        "/{app_id}/deactivate",
        url_name="oauth_apps_deactivate",
        response=schema.OAuthAppSchema,
        throttle=WriteThrottle(),
    )
    def deactivate(self, app_id: int) -> OAuthApplication:
        """Switch an app off and revoke everything it holds."""
        return app_service.set_active(self.get_one(app_id), False)

    @route.post(
        "/{app_id}/activate", url_name="oauth_apps_activate", response=schema.OAuthAppSchema, throttle=WriteThrottle()
    )
    def activate(self, app_id: int) -> OAuthApplication:
        """Switch an app back on. Its users have to authorize it again."""
        return app_service.set_active(self.get_one(app_id), True)
