"""Provision a client (or an agency sub-client) with plan limits, handlers and outbound delivery.

API mode (default; works from anywhere, same validation as the live API):
    python -m scripts.provision_client --api https://autoplatform.<you>.workers.dev --admin-token "$ADMIN_TOKEN" \\
        --slug atlas-living --name "Atlas Living" --plan retainer \\
        --handlers lead_qualifier,support_ticket_router --outbound https://crm.example.com/hooks/autoplatform \\
        --config monetization/configs/real_estate.json --email ops@atlas.example --out atlas.credentials.json

Agency mode (an agency's own API key creates a branded sub-client: /webhook/<agency>-<slug>):
    python -m scripts.provision_client --api https://... --agency-key "$AGENCY_API_KEY" --slug dentist --name "Smile Dental"

Direct DB mode (no API needed; uses DATABASE_URL):
    python -m scripts.provision_client --db --slug atlas-living --name "Atlas Living" --plan retainer

Credentials are printed once. With --out they're written to a file only you can read (chmod 600). Keep them in a password manager.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

INTEGRATIONS = {
    "lead_qualifier": "Typeform / Tally / Jotform / Make 'HTTP' step / your website backend",
    "whatsapp_order_triage": "Meta WhatsApp Cloud API webhook (set meta_verify_token + meta_app_secret in config)",
    "stripe_churn_rescuer": "Stripe Dashboard → Developers → Webhooks (set stripe_webhook_secret in config)",
    "support_ticket_router": "Crisp / Intercom / Help Scout webhooks, or your contact form backend",
    "usage_reporter": "sent automatically every Monday 08:17 UTC to your notify_url / outbound URL",
}


def build_body(a: argparse.Namespace) -> dict[str, Any]:
    body: dict[str, Any] = {"slug": a.slug, "name": a.name}
    if a.plan and not a.agency_key:
        body["plan"] = a.plan
    if a.limit is not None:
        body["monthly_job_limit"] = a.limit
    if a.handlers:
        body["allowed_handlers"] = [h.strip() for h in a.handlers.split(",") if h.strip()]
    if a.outbound:
        body["outbound_webhook_url"] = a.outbound
    config: dict[str, Any] = {}
    if a.config:
        config = json.loads(Path(a.config).read_text())
        if not isinstance(config, dict):
            raise SystemExit("--config must be a JSON object")
    if a.notify:
        config["notify_url"] = a.notify
    if config:
        body["config"] = config
    if a.email:
        body["contact_email"] = a.email
    return body


def provision_via_api(base: str, body: dict, *, admin_token: str | None = None, agency_key: str | None = None,
                      client: httpx.Client | None = None) -> dict:
    http = client or httpx.Client(timeout=30)
    if agency_key:
        r = http.post(f"{base}/agency/clients", json=body, headers={"X-API-Key": agency_key})
    else:
        r = http.post(f"{base}/admin/clients", json=body, headers={"Authorization": f"Bearer {admin_token}"})
    if r.status_code != 201:
        raise SystemExit(f"provisioning failed: HTTP {r.status_code} {r.text[:500]}")
    return r.json()


def provision_via_db(body: dict, base: str) -> dict:
    from app.monetization import credentials
    from db import repo
    from db.migrate import run_migrations

    run_migrations()
    if repo.client_by_slug(body["slug"]):
        raise SystemExit(f"slug {body['slug']!r} already exists")
    c, key = repo.create_client(body["slug"], body["name"], plan=body.get("plan", "retainer"),
                                monthly_job_limit=body.get("monthly_job_limit"),
                                allowed_handlers=body.get("allowed_handlers"),
                                outbound_webhook_url=body.get("outbound_webhook_url"), config=body.get("config"),
                                contact_email=body.get("contact_email"))
    return credentials(c, key, base)


def report(creds: dict, out: str | None) -> None:
    print(f"\n✅ {creds['slug']} provisioned on plan {creds['plan']} ({creds['monthly_job_limit']} jobs/month)\n")
    print(f"  API key (X-API-Key) : {creds.get('api_key', '(unchanged)')}")
    print(f"  Webhook secret      : {creds.get('webhook_secret', '(unchanged)')}")
    print(f"  Generic endpoint    : POST {creds['webhook_url']}  {{\"type\": <handler>, \"payload\": {{...}}}}\n")
    print("  Signed endpoints per integration:")
    for name, url in creds.get("handler_urls", {}).items():
        print(f"    {name:24s} {url}\n    {'':24s} ↳ {INTEGRATIONS.get(name, 'any HMAC-SHA256 signed sender')}")
    if out:
        path = Path(out)
        path.write_text(json.dumps(creds, indent=2))
        os.chmod(path, 0o600)
        print(f"\n  Credentials written to {path} (chmod 600). Store them in a password manager, then delete the file.")
    print("\n  The API key is shown once; it can't be recovered, only rotated.\n")


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", help="base URL of the deployment (API mode)")
    p.add_argument("--admin-token", default=os.getenv("ADMIN_TOKEN"))
    p.add_argument("--agency-key", help="agency API key: creates a sub-client under that agency")
    p.add_argument("--db", action="store_true", help="write directly to DATABASE_URL instead of calling the API")
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--plan", default="retainer", help="retainer | agency | guard_starter | guard_pro")
    p.add_argument("--limit", type=int, help="monthly job limit (overrides the plan)")
    p.add_argument("--handlers", help="comma-separated allowed handlers (overrides the plan)")
    p.add_argument("--outbound", help="https URL that receives signed results (CRM, WhatsApp gateway, Make/Zapier hook)")
    p.add_argument("--notify", help="Slack/Discord webhook for human-readable summaries and weekly reports")
    p.add_argument("--config", help="path to a JSON config (see monetization/configs/)")
    p.add_argument("--email", help="contact email")
    p.add_argument("--out", help="write credentials to this file (chmod 600)")
    return p


def main(argv: list[str] | None = None) -> int:
    p = make_parser()
    a = p.parse_args(argv)
    body = build_body(a)
    if a.db:
        creds = provision_via_db(body, (a.api or os.getenv("PUBLIC_BASE_URL") or "https://<your-host>").rstrip("/"))
    else:
        if not a.api:
            p.error("--api is required unless --db is used")
        if not (a.admin_token or a.agency_key):
            p.error("--admin-token (or ADMIN_TOKEN) or --agency-key is required")
        creds = provision_via_api(a.api.rstrip("/"), body, admin_token=a.admin_token, agency_key=a.agency_key)
    report(creds, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
