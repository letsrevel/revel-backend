"""ninja-jwt token backend that also accepts tokens signed with a previous SECRET_KEY."""

import typing as t

from jwt import InvalidAlgorithmError, InvalidTokenError
from ninja_jwt.backends import TokenBackend
from ninja_jwt.exceptions import TokenBackendError

from common.keys import decode_jwt, verification_keys

__all__ = ["FallbackTokenBackend"]


class FallbackTokenBackend(TokenBackend):
    """``TokenBackend`` whose HS* decoding tries every key from ``verification_keys()``."""

    def decode(self, token: str, verify: bool = True) -> dict[str, t.Any]:
        """Decode like the parent, falling back to older keys on a signature mismatch only.

        Raises:
            TokenBackendError: The token is malformed, expired or not verifiable with any key.
        """
        if not verify or not self.algorithm.startswith("HS"):
            return super().decode(token, verify=verify)
        try:
            return decode_jwt(
                token,
                verification_keys(),
                algorithms=[self.algorithm],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.get_leeway(),
                options={"verify_aud": self.audience is not None, "verify_signature": True},
            )
        except InvalidAlgorithmError as ex:
            raise TokenBackendError("Invalid algorithm specified") from ex
        except InvalidTokenError as ex:
            raise TokenBackendError("Token is invalid or expired") from ex
