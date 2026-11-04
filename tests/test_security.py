"""Security hardening: body limits, metrics auth, SSRF guards (incl. DNS rebinding)."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from workers import handlers
from workers.handlers import JobContext, PermanentError

ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.fixture
def plain_api(settings, make_router):
    app = create_app(replace(settings, run_embedded_worker=False, max_body_bytes=10_000), router=make_router())
    with TestClient(app) as c:
        yield c


def test_oversized_body_rejected_by_content_length(plain_api):
    r = plain_api.post("/webhook", content=b"x" * 20_000, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_oversized_chunked_body_rejected_while_streaming(plain_api):
    def gen():
        for _ in range(30):
            yield b"x" * 1000

    r = plain_api.post("/webhook/acme", content=gen(), headers={"X-Signature-256": "sha256=00"})
    assert r.status_code == 413


def test_small_bodies_still_work(plain_api):
    assert plain_api.post("/webhook", json={"type": "echo"}).status_code == 401  # reaches auth


def test_metrics_locked_in_production(settings, make_router):
    app = create_app(replace(settings, app_env="production", run_embedded_worker=False), router=make_router())
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/metrics", headers=ADMIN).status_code == 200
    app = create_app(replace(settings, app_env="production", metrics_public=True,
                             run_embedded_worker=False), router=make_router())
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 200


@pytest.mark.parametrize("ip,public", [("8.8.8.8", True), ("127.0.0.1", False), ("10.1.2.3", False),
                                       ("169.254.169.254", False), ("::1", False),
                                       ("::ffff:127.0.0.1", False), ("100.64.0.1", False),
                                       ("2606:4700:4700::1111", True)])
def test_public_ip_classifier(ip, public):
    assert handlers._is_public(ip) is public


def test_precheck_rejects_bad_urls():
    for url in ("http://example.com", "https://user:pw@example.com/", "https://127.0.0.1/", "ftp://x"):
        with pytest.raises(PermanentError):
            handlers._assert_public_https(url)


async def test_dns_rebinding_blocked_at_connect(monkeypatch, settings, make_router):
    """Pretend DNS said 'public' during the pre-check but the connection lands on 127.0.0.1."""
    got_bytes = []

    async def on_conn(reader, writer):
        got_bytes.append(await asyncio.wait_for(reader.read(100), 5))
        writer.close()

    server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(handlers, "_assert_public_https", lambda url: None)  # rebinding: check passed
    ctx = JobContext("j", None, 1, "w", make_router(), None, settings)
    with pytest.raises(PermanentError, match="non-public address 127.0.0.1"):
        await handlers.HANDLERS["http.fetch"](ctx, {"url": f"https://127.0.0.1:{port}/"})
    for _ in range(50):
        if got_bytes:
            break
        await asyncio.sleep(0.05)
    server.close()
    await server.wait_closed()
    assert got_bytes == [b""]  # connection closed (EOF) without a single byte, not even a TLS hello
