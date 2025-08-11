from django.contrib.gis.geos import Point
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from geo.ip2 import LazyGeoPoint
from geo.middleware import GeoPointMiddleware


def test_geo_point_middleware_x_forwarded_for() -> None:
    """
    Tests that the middleware correctly extracts the IP from the
    HTTP_X_FORWARDED_FOR header and attaches a LazyGeoPoint to the request.
    """
    factory = RequestFactory()
    request: HttpRequest = factory.get("/", HTTP_X_FORWARDED_FOR="8.8.8.8, 1.1.1.1")

    def get_response(req: HttpRequest) -> HttpResponse:
        return HttpResponse()

    middleware = GeoPointMiddleware(get_response)
    middleware(request)

    assert hasattr(request, "user_location")
    user_location = getattr(request, "user_location", None)
    assert isinstance(user_location, LazyGeoPoint)
    assert user_location.ip == "8.8.8.8"

    point = user_location.get()
    assert isinstance(point, Point)
    assert point.x == 16.3738
    assert point.y == 48.2082


def test_geo_point_middleware_remote_addr() -> None:
    """
    Tests that the middleware falls back to REMOTE_ADDR when
    HTTP_X_FORWARDED_FOR is not present.
    """
    factory = RequestFactory()
    request: HttpRequest = factory.get("/", REMOTE_ADDR="8.8.4.4")

    def get_response(req: HttpRequest) -> HttpResponse:
        return HttpResponse()

    middleware = GeoPointMiddleware(get_response)
    middleware(request)

    assert hasattr(request, "user_location")
    user_location = getattr(request, "user_location", None)
    assert isinstance(user_location, LazyGeoPoint)
    assert user_location.ip == "8.8.4.4"

    point = user_location.get()
    assert isinstance(point, Point)


def test_geo_point_middleware_no_ip() -> None:
    """
    Tests that the middleware handles requests with no IP address gracefully.
    """
    factory = RequestFactory()
    request: HttpRequest = factory.get("/", REMOTE_ADDR="")

    def get_response(req: HttpRequest) -> HttpResponse:
        return HttpResponse()

    middleware = GeoPointMiddleware(get_response)
    middleware(request)

    assert hasattr(request, "user_location")
    user_location = getattr(request, "user_location", None)
    assert isinstance(user_location, LazyGeoPoint)
    assert user_location.ip == ""
    assert user_location.get() is None
