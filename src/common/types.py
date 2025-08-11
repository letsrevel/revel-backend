"""Common types."""

from django.http import HttpRequest as DjangoHttpRequest

from accounts.models import RevelUser
from geo.ip2 import LazyGeoPoint


class HttpRequest(DjangoHttpRequest):
    user: RevelUser
    user_location: LazyGeoPoint
