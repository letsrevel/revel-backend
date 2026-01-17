"""Media validation endpoint for Caddy forward_auth.

This controller provides the validation endpoint that Caddy calls via
its forward_auth directive to verify signed URLs for protected files.

Flow:
    1. Client requests /media/file/abc.pdf?exp=...&sig=...
    2. Caddy's forward_auth calls /api/media/validate/file/abc.pdf?exp=...&sig=...
    3. This endpoint verifies the signature and expiry
    4. Returns 200 (Caddy serves file) or 401 (access denied)

Note:
    This endpoint is NOT authenticated - it validates the HMAC signature instead.
    Rate limiting is applied to prevent brute-force attacks on signatures.
"""

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja_extra import api_controller, route

from common.signing import parse_signed_url_params, verify_signature
from common.throttling import MediaValidationThrottle


@api_controller("/media", tags=["Media"])
class MediaValidationController:
    """Controller for validating signed media URLs.

    This is called by Caddy's forward_auth directive, not by end users directly.
    The throttle is set higher than typical endpoints because Caddy is a single
    IP making all validation requests, and pages may load multiple protected
    files simultaneously.
    """

    @route.get(
        "/validate/{path:path}",
        url_name="validate_media",
        response={200: None, 401: None},
        throttle=MediaValidationThrottle(),
        exclude_unset=True,
    )
    def validate_media(self, request: HttpRequest, path: str) -> HttpResponse:
        """Validate a signed URL for protected media access.

        Called by Caddy's forward_auth directive. Validates the HMAC signature
        and expiration timestamp. Returns 200 to allow access, 401 to deny.

        The full path being validated is reconstructed as /media/{path}
        to match what was signed when the URL was generated.

        Args:
            request: The HTTP request from Caddy's forward_auth.
            path: The file path being requested (captured from URL).

        Returns:
            200 response if signature valid and not expired.
            401 response if invalid signature, expired, or missing params.
        """
        exp = request.GET.get("exp")
        sig = request.GET.get("sig")

        # Reconstruct the full path that was signed
        # The signed path format is /media/{path}
        media_url = settings.MEDIA_URL.rstrip("/")
        full_path = f"{media_url}/{path}"

        params = parse_signed_url_params(full_path, exp, sig)
        if params is None:
            return HttpResponse(status=401)

        if not verify_signature(params.path, params.exp, params.sig):
            return HttpResponse(status=401)

        return HttpResponse(status=200)
