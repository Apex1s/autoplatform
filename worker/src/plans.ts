// Commercial plans: job quotas + which handlers each tier may run (mirrors core/plans.py).
export const PRODUCT_HANDLERS = ["lead_qualifier", "whatsapp_order_triage", "stripe_churn_rescuer", "support_ticket_router"];

export interface Plan {
  label: string;
  price_usd: number;
  monthly_job_limit: number | null;
  handlers: string[] | null; // null = every client-callable handler
  max_sub_clients?: number;
  sub_client_limit?: number;
}

export const PLANS: Record<string, Plan> = {
  retainer: { label: "Speed-to-Lead & Ops Retainer", price_usd: 1000, monthly_job_limit: 20000, handlers: null },
  agency: { label: "White-Label Agency Engine", price_usd: 500, monthly_job_limit: 50000, handlers: null,
            max_sub_clients: 25, sub_client_limit: 5000 },
  agency_client: { label: "Agency sub-client", price_usd: 0, monthly_job_limit: 5000, handlers: null },
  guard_starter: { label: "AI Webhook Guard - Starter", price_usd: 99, monthly_job_limit: 2000,
                   handlers: ["support_ticket_router", "stripe_churn_rescuer", "webhook.event", "usage_reporter"] },
  guard_pro: { label: "AI Webhook Guard - Pro", price_usd: 249, monthly_job_limit: 10000,
               handlers: ["support_ticket_router", "stripe_churn_rescuer", "lead_qualifier", "webhook.event",
                          "http.fetch", "usage_reporter"] },
  demo: { label: "Live demo", price_usd: 0, monthly_job_limit: 3000, handlers: PRODUCT_HANDLERS },
};

export const DEFAULT_PLAN = "retainer";
export const RESERVED_SLUGS = new Set(["stripe", "demo", "demo-admin", "admin", "api", "www", "billing", "agency"]);

export const plan = (name: string | null | undefined): Plan => PLANS[name ?? DEFAULT_PLAN] ?? PLANS[DEFAULT_PLAN];

export const effectiveLimit = (p: string | null | undefined, override: number | null | undefined) =>
  override ?? plan(p).monthly_job_limit;

export const effectiveHandlers = (p: string | null | undefined, override: string[] | null | undefined) =>
  override ?? plan(p).handlers;

export function planForAmount(amountCents: number | null | undefined, currency: string | null | undefined): string | null {
  if (amountCents == null || (currency ?? "usd").toLowerCase() !== "usd") return null;
  for (const [name, p] of Object.entries(PLANS)) if (p.price_usd && p.price_usd * 100 === amountCents) return name;
  return null;
}
