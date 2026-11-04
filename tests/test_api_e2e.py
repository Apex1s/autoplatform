"""End-to-end through HTTP: API -> Redis queue -> embedded worker -> DB, with mocked LLMs."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"Authorization": "Bearer test-admin"}
CRON = {"Authorization": "Bearer test-cron"}


@pytest.fixture
def api(settings, make_router, fake_llm):
    import asyncio

    async def real_sleep(s):  # job-level retries need real (tiny) sleeps
        await asyncio.sleep(min(s, 0.01))

    router = make_router(sleep=real_sleep, usage_hook=lambda rec: asyncio.to_thread(
        __import__("db.repo", fromlist=["x"]).record_usage, rec))
    app = create_app(settings, router=router)
    with TestClient(app) as c:
        c.fake_llm = fake_llm
        c.router = router
        yield c


@pytest.fixture
def client_creds(api):
    r = api.post("/admin/clients", json={"slug": "acme", "name": "Acme Inc"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def wait_job(api, job_id, headers, terminal=("succeeded", "failed"), timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = api.get(f"/tasks/{job_id}", headers=headers).json()
        if j["status"] in terminal:
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} stuck: {j}")


def test_health_live_root(api):
    h = api.get("/healthz")
    assert h.status_code == 200, h.text
    body = h.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok" and body["checks"]["redis"] == "ok"
    assert body["checks"]["worker"] == "ok"
    assert api.get("/livez").json() == {"status": "alive"}
    assert "llm.complete" in api.get("/").json()["job_types"]


def test_admin_auth(api):
    assert api.post("/admin/clients", json={"slug": "x-co", "name": "X"}).status_code == 401
    assert api.post("/admin/clients", json={"slug": "x-co", "name": "X"},
                    headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.post("/admin/clients", json={"slug": "Bad Slug", "name": "X"}, headers=ADMIN).status_code == 422


def test_duplicate_slug(api, client_creds):
    assert api.post("/admin/clients", json={"slug": "acme", "name": "dup"}, headers=ADMIN).status_code == 409
    assert [c["slug"] for c in api.get("/admin/clients", headers=ADMIN).json()] == ["acme"]


def test_webhook_auth_and_validation(api, client_creds):
    key = {"X-API-Key": client_creds["api_key"]}
    assert api.post("/webhook", json={"type": "echo"}).status_code == 401
    assert api.post("/webhook", json={"type": "echo"}, headers={"X-API-Key": "nope"}).status_code == 401
    assert api.post("/webhook", json={"type": "nope"}, headers=key).status_code == 422
    assert api.post("/webhook", json={"type": "system.maintenance"}, headers=key).status_code == 422


def test_echo_job_end_to_end(api, client_creds):
    key = {"X-API-Key": client_creds["api_key"]}
    r = api.post("/webhook", json={"type": "echo", "payload": {"hello": "world"}}, headers=key)
    assert r.status_code == 202
    assert r.json()["status_url"].endswith(r.json()["job_id"])
    job = wait_job(api, r.json()["job_id"], key)
    assert job["status"] == "succeeded" and job["result"]["echo"] == {"hello": "world"}
    assert [j["id"] for j in api.get("/tasks", headers=key).json()] == [job["id"]]


def test_llm_job_fails_over_on_429(api, client_creds):
    key = {"X-API-Key": client_creds["api_key"]}
    api.fake_llm.script("groq", (429, {"retry-after": "30"}, None))
    r = api.post("/webhook", json={"type": "llm.complete", "payload": {"prompt": "Say hi"}}, headers=key)
    job = wait_job(api, r.json()["job_id"], key)
    assert job["status"] == "succeeded", job
    assert job["result"]["provider"] == "gemini"
    assert job["result"]["text"] == "hello from gemini"
    assert [a["error_kind"] for a in job["result"]["attempts"]] == ["rate_limit", None]

    m = api.get("/metrics?format=json").json()
    assert m["llm_since_boot"]["groq"]["rate_limited"] == 1
    usage = {(u["provider"], u["success"]): u for u in m["llm_usage_24h"]}
    assert usage[("groq", False)]["calls"] == 1
    assert usage[("gemini", True)]["prompt_tokens"] == 11
    text = api.get("/metrics").text
    assert 'autoplatform_llm_rate_limited_total{provider="groq"} 1' in text
    assert 'autoplatform_jobs{status="succeeded"} 1' in text
    assert "autoplatform_worker_up{" in text


def test_job_retries_then_succeeds(api, client_creds):
    key = {"X-API-Key": client_creds["api_key"]}
    r = api.post("/webhook", json={"type": "echo", "payload": {"fail_times": 1}}, headers=key)
    job = wait_job(api, r.json()["job_id"], key)
    assert job["status"] == "succeeded" and job["attempts"] == 2


def test_job_exhausts_attempts(api, client_creds):
    key = {"X-API-Key": client_creds["api_key"]}
    r = api.post("/webhook", json={"type": "echo", "payload": {"fail_times": 9}, "max_attempts": 2},
                 headers=key)
    job = wait_job(api, r.json()["job_id"], key)
    assert job["status"] == "failed" and job["attempts"] == 2 and "simulated failure" in job["error"]


def test_permanent_error_no_retry(api, client_creds):
    key = {"X-API-Key": client_creds["api_key"]}
    r = api.post("/webhook", json={"type": "llm.complete", "payload": {}}, headers=key)
    job = wait_job(api, r.json()["job_id"], key)
    assert job["status"] == "failed" and job["attempts"] == 1 and "PermanentError" in job["error"]


def test_all_providers_down_job_fails_cleanly(api, client_creds):
    key = {"X-API-Key": client_creds["api_key"]}
    api.fake_llm.script("groq", *[401] * 10)
    api.fake_llm.script("gemini", *[401] * 10)
    r = api.post("/webhook", json={"type": "llm.complete", "payload": {"prompt": "x"}, "max_attempts": 1},
                 headers=key)
    job = wait_job(api, r.json()["job_id"], key)
    assert job["status"] == "failed" and "all LLM providers failed" in job["error"]


def test_idempotency_key(api, client_creds):
    h = {"X-API-Key": client_creds["api_key"], "Idempotency-Key": "order-42"}
    a = api.post("/webhook", json={"type": "echo"}, headers=h).json()
    b = api.post("/webhook", json={"type": "echo"}, headers=h).json()
    assert b["duplicate"] is True and b["job_id"] == a["job_id"]


def test_tenant_isolation(api, client_creds):
    other = api.post("/admin/clients", json={"slug": "globex", "name": "Globex"}, headers=ADMIN).json()
    r = api.post("/webhook", json={"type": "echo"}, headers={"X-API-Key": client_creds["api_key"]})
    assert api.get(f"/tasks/{r.json()['job_id']}", headers={"X-API-Key": other["api_key"]}).status_code == 404


def test_signed_webhook(api, client_creds):
    body = json.dumps({"action": "opened", "llm_prompt": "summarize"}).encode()
    sig = "sha256=" + hmac.new(client_creds["webhook_secret"].encode(), body, hashlib.sha256).hexdigest()
    bad = api.post("/webhook/acme", content=body, headers={"X-Signature-256": "sha256=deadbeef"})
    assert bad.status_code == 401
    assert api.post("/webhook/nobody", content=body, headers={"X-Signature-256": sig}).status_code == 401
    ok = api.post("/webhook/acme", content=body,
                  headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request",
                           "Content-Type": "application/json"})
    assert ok.status_code == 202, ok.text
    job = wait_job(api, ok.json()["job_id"], {"X-API-Key": client_creds["api_key"]})
    assert job["result"]["event"] == "pull_request"
    assert job["result"]["keys"] == ["action", "llm_prompt"]
    assert job["result"]["llm"]["provider"] == "groq"


def test_cron_endpoints(api):
    assert api.post("/cron/maintenance").status_code == 401
    assert api.post("/cron/unknown", headers=CRON).status_code == 404
    r = api.post("/cron/maintenance", headers=CRON)
    assert r.status_code == 202 and "queue" in r.json()
    jid = r.json()["job_id"]
    deadline = time.time() + 10
    while (j := api.get(f"/cron/jobs/{jid}", headers=CRON).json())["status"] not in ("succeeded", "failed"):
        assert time.time() < deadline
        time.sleep(0.05)
    assert j["status"] == "succeeded", j
    assert set(j["result"]) >= {"dead_workers", "requeued_stuck", "pruned"}

    p = api.post("/cron/provider_probe", headers=ADMIN)  # admin token also works
    assert p.status_code == 202
    jid = p.json()["job_id"]
    deadline = time.time() + 10
    while (j := api.get(f"/cron/jobs/{jid}", headers=CRON).json())["status"] not in ("succeeded", "failed"):
        assert time.time() < deadline
        time.sleep(0.05)
    assert j["result"]["providers"]["groq"]["ok"] and j["result"]["providers"]["gemini"]["ok"]


def test_http_fetch_blocks_private_targets(api, client_creds):
    key = {"X-API-Key": client_creds["api_key"]}
    for url in ("http://example.com", "https://127.0.0.1/admin", "https://localhost/"):
        r = api.post("/webhook", json={"type": "http.fetch", "payload": {"url": url}}, headers=key)
        job = wait_job(api, r.json()["job_id"], key)
        assert job["status"] == "failed" and job["attempts"] == 1, (url, job)
        assert "PermanentError" in job["error"]


def test_metrics_token(settings, make_router):
    from dataclasses import replace
    app = create_app(replace(settings, metrics_token="m", run_embedded_worker=False), router=make_router())
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer m"}).status_code == 200
        assert c.get("/healthz").json()["checks"]["worker"] == "external"


def test_deactivate_client(api, client_creds):
    key = {"X-API-Key": client_creds["api_key"]}
    assert api.post("/webhook", json={"type": "echo"}, headers=key).status_code == 202
    assert api.delete("/admin/clients/acme").status_code == 401
    assert api.delete("/admin/clients/acme", headers=ADMIN).json() == {"slug": "acme", "active": False}
    assert api.post("/webhook", json={"type": "echo"}, headers=key).status_code == 401  # key revoked
    assert api.delete("/admin/clients/acme", headers=ADMIN).status_code == 404


def test_metrics_reports_live_workers(api):
    m = api.get("/metrics?format=json").json()
    assert len(m["workers"]) == 1 and m["workers"][0]["status"] == "alive"
    assert m["workers_awaiting_reap"] == []
    assert "autoplatform_workers_alive 1" in api.get("/metrics").text
