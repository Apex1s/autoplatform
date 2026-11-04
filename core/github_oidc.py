"""Keyless auth for GitHub Actions -> /cron/* using GitHub's OIDC tokens.

Each workflow run asks GitHub for a short-lived (~5 min) RS256 JWT with
`permissions: id-token: write`. We verify it against GitHub's published JWKS and pin it to
one repository + branch + audience, so no long-lived cron secret has to live in GitHub.

Claims checked: signature (RS256 only), iss, aud, exp/iat (+30s leeway), repository, ref,
and event_name (schedule / workflow_dispatch).
"""
from __future__ import annotations

import jwt
from jwt import PyJWKClient

ISSUER = "https://token.actions.githubusercontent.com"
ALLOWED_EVENTS = {"schedule", "workflow_dispatch"}

_jwks_client: PyJWKClient | None = None


class OIDCError(Exception):
    pass


def _client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(f"{ISSUER}/.well-known/jwks", cache_keys=True, lifespan=3600,
                                   timeout=10)
    return _jwks_client


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and token.startswith("ey")


def verify(token: str, *, repository: str, audience: str, ref: str | None = "refs/heads/main",
           jwks_client: PyJWKClient | None = None) -> dict:
    """Returns the verified claims or raises OIDCError. Blocking (JWKS fetch): call via to_thread."""
    try:
        key = (jwks_client or _client()).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"], audience=audience, issuer=ISSUER, leeway=30,
            options={"require": ["exp", "iat", "iss", "aud", "repository"]},
        )
    except Exception as e:  # PyJWTError, PyJWKClientError, network errors
        raise OIDCError(f"invalid GitHub OIDC token: {type(e).__name__}: {e}") from e
    if str(claims.get("repository", "")).lower() != repository.lower():
        raise OIDCError(f"token is for repository {claims.get('repository')!r}")
    if ref and claims.get("ref") != ref:
        raise OIDCError(f"token is for ref {claims.get('ref')!r}, expected {ref!r}")
    if claims.get("event_name") not in ALLOWED_EVENTS:
        raise OIDCError(f"event {claims.get('event_name')!r} may not trigger cron jobs")
    return claims
