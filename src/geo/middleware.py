import typing as t
from typing import Callable

from django.http import HttpRequest, HttpResponse

from geo.ip2 import LazyGeoPoint


class GeoPointMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Init."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Add the user location to the request."""
        ip = self._get_ip(request)
        request.user_location = LazyGeoPoint(ip)  # type: ignore[attr-defined]
        return self.get_response(request)

    @staticmethod
    def _get_ip(request: HttpRequest) -> str:
        # return "188.23.209.25"
        # return "63.116.61.253"
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            return t.cast(str, xff.split(",")[0].strip())
        return t.cast(str, request.META.get("REMOTE_ADDR", ""))
