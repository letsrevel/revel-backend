import typing as t

from django.http import HttpRequest, HttpResponse

from common.client_ip import get_client_ip
from geo.ip2 import LazyGeoPoint


class GeoPointMiddleware:
    def __init__(self, get_response: t.Callable[[HttpRequest], HttpResponse]) -> None:
        """Init."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Add the user location to the request."""
        ip = get_client_ip(request)
        request.user_location = LazyGeoPoint(ip)  # type: ignore[attr-defined]
        return self.get_response(request)
