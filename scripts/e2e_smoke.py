"""Black-box smoke test against a running deployment (local or remote).

    python -m scripts.e2e_smoke --base-url https://<space>.hf.space --admin-token $ADMIN_TOKEN [--llm]

Creates (or reuses) a 'smoke-test' client, pushes jobs through /webhook and /webhook/{slug},
fires /cron/maintenance, and checks /healthz + /metrics. Exit 0 = all green.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--admin-token", required=True)
    ap.add_argument("--slug", default=f"smoke-{int(time.time())}")
    ap.add_argument("--llm", action="store_true", help="also run a real llm.complete job")
    ap.add_argument("--timeout", type=float, default=90)
    a = ap.parse_args()
    base = a.base_url.rstrip("/")
    admin = {"Authorization": f"Bearer {a.admin_token}"}
    results: list[tuple[str, bool, str]] = []
    http = httpx.Client(timeout=30)

    def check(name, ok, detail=""):
        results.append((name, bool(ok), str(detail)[:300]))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {str(detail)[:200]}")
        return ok

    def wait(job_id, headers):
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            j = http.get(f"{base}/tasks/{job_id}", headers=headers).json()
            if j.get("status") in ("succeeded", "failed"):
                return j
            time.sleep(1)
        return j

    # Cold starts (HF Spaces wake-up) can take a while: retry healthz.
    for i in range(30):
        try:
            h = http.get(f"{base}/healthz")
            if h.status_code == 200:
                break
        except httpx.HTTPError as e:
            h = e
        time.sleep(5)
    hj = h.json() if isinstance(h, httpx.Response) else {}
    check("healthz 200 + status ok", isinstance(h, httpx.Response) and h.status_code == 200
          and hj.get("status") == "ok", hj)

    c = http.post(f"{base}/admin/clients", json={"slug": a.slug, "name": "Smoke Test"}, headers=admin)
    if not check("create client", c.status_code == 201, c.status_code):
        return 1
    creds = c.json()
    key = {"X-API-Key": creds["api_key"]}

    r = http.post(f"{base}/webhook", json={"type": "echo", "payload": {"ping": 1}}, headers=key)
    j = wait(r.json()["job_id"], key)
    check("echo job via /webhook", j.get("status") == "succeeded", j.get("result"))

    body = json.dumps({"event": "smoke"}).encode()
    sig = "sha256=" + hmac.new(creds["webhook_secret"].encode(), body, hashlib.sha256).hexdigest()
    r = http.post(f"{base}/webhook/{a.slug}", content=body,
                  headers={"X-Signature-256": sig, "Content-Type": "application/json"})
    ok = r.status_code == 202
    j = wait(r.json()["job_id"], key) if ok else {}
    check("signed webhook /webhook/{slug}", ok and j.get("status") == "succeeded", j.get("result") or r.text)

    if a.llm:
        r = http.post(f"{base}/webhook", json={"type": "llm.complete",
                      "payload": {"prompt": "Reply with exactly: pong", "max_tokens": 10}}, headers=key)
        j = wait(r.json()["job_id"], key)
        res = j.get("result") or {}
        check("llm.complete job", j.get("status") == "succeeded",
              {"provider": res.get("provider"), "text": res.get("text"),
               "attempts": [(x["provider"], x["error_kind"]) for x in res.get("attempts", [])],
               "error": (j.get("error") or "")[:150]})

    r = http.post(f"{base}/cron/maintenance", headers=admin)
    check("cron/maintenance accepted", r.status_code == 202, r.json())

    m = http.get(f"{base}/metrics", headers=admin)
    check("metrics exposes jobs + workers", m.status_code == 200 and "autoplatform_jobs{" in m.text
          and "autoplatform_worker_up{" in m.text, f"{len(m.text)} bytes")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
