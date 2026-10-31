"""Cognito ID-token verification; importing this module makes no AWS calls."""

import jwt


class AuthError(Exception):
    """The supplied token could not be verified as an ID token."""


def cognito_issuer(region, pool_id):
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"


class JwksKeys:
    """Cache the pool's signing keys. Only key_for can fetch keys from AWS."""

    def __init__(self, issuer):
        self.client = jwt.PyJWKClient(
            issuer + "/.well-known/jwks.json", cache_keys=True,
        )

    def key_for(self, token):
        return self.client.get_signing_key_from_jwt(token).key


def verify_id_token(token, *, key_for, issuer, client_id, leeway=30) -> dict:
    # Key lookup belongs inside the try: JWKS errors are PyJWT errors too.
    try:
        claims = jwt.decode(
            token,
            key_for(token),
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuer,
            leeway=leeway,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError("Invalid ID token.") from exc

    if claims.get("token_use") != "id":
        raise AuthError("Expected an ID token.")
    return claims
