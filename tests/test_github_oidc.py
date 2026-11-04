"""Keyless cron auth: GitHub Actions OIDC tokens, verified against a JWKS we control in tests."""
from __future__ import annotations

import time
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.main import create_app
from core import github_oidc

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
REPO = "Apex1s/autoplatform"


class FakeJWKS:
    def get_signing_key_from_jwt(self, token):
        class K:
            key = KEY.public_key()
        return K()


def mint(key=KEY, **overrides):
    now = int(time.time())
    claims = {"iss": github_oidc.ISSUER, "aud": "autoplatform-cron", "iat": now, "nbf": now,
              "exp": now + 300, "repository": REPO, "ref": "refs/heads/main",
              "event_name": "schedule", "workflow_ref": f"{REPO}/.github/workflows/x.yml@refs/heads/main",
              "run_id": "1"}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256",
                      headers={"kid": "test"})


@pytest.fixture
def oidc_api(settings, make_router, monkeypatch):
    monkeypatch.setattr(github_oidc, "_jwks_client", FakeJWKS())
    s = replace(settings, cron_oidc_repository=REPO, run_embedded_worker=False)
    with TestClient(create_app(s, router=make_router())) as c:
        yield c


def test_valid_token_accepted(oidc_api):
    r = oidc_api.post("/cron/maintenance", headers={"Authorization": f"Bearer {mint()}"})
    assert r.status_code == 202, r.text


def test_workflow_dispatch_accepted(oidc_api):
    tok = mint(event_name="workflow_dispatch")
    assert oidc_api.post("/cron/maintenance", headers={"Authorization": f"Bearer {tok}"}).status_code == 202


@pytest.mark.parametrize("bad", [
    {"repository": "evil/autoplatform"},           # another repo's workflow
    {"ref": "refs/heads/feature"},                  # not main
    {"aud": "someone-else"},                        # token minted for another service
    {"iss": "https://evil.example.com"},            # wrong issuer
    {"exp": int(time.time()) - 120},                # expired (beyond 30s leeway)
    {"event_name": "pull_request"},                 # PR workflows can't fire cron jobs
    {"repository": None},                           # required claim missing
])
def test_bad_claims_rejected(oidc_api, bad):
    r = oidc_api.post("/cron/maintenance", headers={"Authorization": f"Bearer {mint(**bad)}"})
    assert r.status_code == 401


def test_wrong_signing_key_rejected(oidc_api):
    r = oidc_api.post("/cron/maintenance", headers={"Authorization": f"Bearer {mint(key=OTHER_KEY)}"})
    assert r.status_code == 401


def test_alg_none_and_hs256_rejected(oidc_api):
    now = int(time.time())
    claims = {"iss": github_oidc.ISSUER, "aud": "autoplatform-cron", "iat": now, "exp": now + 300,
              "repository": REPO, "ref": "refs/heads/main", "event_name": "schedule"}
    none_tok = jwt.encode(claims, key=None, algorithm="none")
    hs_tok = jwt.encode(claims, "guessable-secret-of-32-bytes-len!", algorithm="HS256")
    for tok in (none_tok, hs_tok):
        assert oidc_api.post("/cron/maintenance", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_static_cron_token_still_works(oidc_api):
    assert oidc_api.post("/cron/maintenance", headers={"Authorization": "Bearer test-cron"}).status_code == 202
    assert oidc_api.post("/cron/maintenance", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert oidc_api.post("/cron/maintenance").status_code == 401


def test_real_github_jwks_reachable_and_rejects_forged_token():
    """Live check of the JWKS endpoint (skipped offline): a self-signed token must fail."""
    try:
        with pytest.raises(github_oidc.OIDCError):
            github_oidc.verify(mint(), repository=REPO, audience="autoplatform-cron",
                               jwks_client=jwt.PyJWKClient(f"{github_oidc.ISSUER}/.well-known/jwks", timeout=10))
    except Exception as e:  # network unavailable
        pytest.skip(f"no network: {e}")
