"""URL configuration for revel project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/

Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect, reverse  # type: ignore[attr-defined]
from django.urls import include, path

from api.api import api

admin.site.name = f"{settings.SITE_NAME} v{settings.VERSION}"
admin.site.index_title = f"Welcome to {settings.SITE_NAME} v{settings.VERSION} Admin"
admin.site.site_title = f"{settings.SITE_NAME} v{settings.VERSION} Admin"


def redirect_to_docs(request: HttpRequest) -> HttpResponseRedirect:
    """Redirect to the API documentation."""
    return redirect(reverse("api:openapi-view"))


urlpatterns = [
    path("api/", api.urls),
    path("google_sso/", include("django_google_sso.urls", namespace="django_google_sso")),
    path("", include("django_prometheus.urls")),  # Prometheus metrics endpoint at /metrics Caddy will return 404
]

if settings.ADMIN_URL:  # pragma: no cover
    urlpatterns.insert(1, path(settings.ADMIN_URL, admin.site.urls))

if settings.DEBUG:
    urlpatterns.insert(1, path("", redirect_to_docs, name="redirect_to_docs"))  # type: ignore[arg-type]
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)  # type: ignore[arg-type]


if settings.SILK_PROFILER:
    urlpatterns += [path("silk/", include("silk.urls", namespace="silk"))]
