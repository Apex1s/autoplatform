// Configuration: Cloudflare bindings (vars + secrets) -> typed Config.
export interface Env {
  DATABASE_URL?: string;
  UPSTASH_REDIS_URL?: string; // rediss://default:TOKEN@HOST:6379 (REST URL/token derived from it)
  UPSTASH_REDIS_REST_URL?: string;
  UPSTASH_REDIS_REST_TOKEN?: string;
  GROQ_API_KEY?: string;
  GEMINI_API_KEY?: string;
  ADMIN_TOKEN?: string;
  CRON_TOKEN?: string;
  METRICS_TOKEN?: string;
  METRICS_PUBLIC?: string;
  APP_ENV?: string;
  PUBLIC_BASE_URL?: string;
  QUEUE_PREFIX?: string;
  GROQ_MODEL?: string;
  GEMINI_MODEL?: string;
  GROQ_BASE_URL?: string;
  GEMINI_BASE_URL?: string;
  LLM_PROVIDER_ORDER?: string;
  LLM_TIMEOUT_SECONDS?: string;
  LLM_MAX_RETRIES?: string;
  LLM_MAX_ROUNDS?: string;
  LLM_DEFAULT_COOLDOWN?: string;
  JOB_MAX_ATTEMPTS?: string;
  JOB_RETRY_BASE_SECONDS?: string;
  JOB_TIMEOUT_SECONDS?: string;
  LOG_RETENTION_DAYS?: string;
  MAX_BODY_BYTES?: string;
  SUBREQUEST_BUDGET?: string;
}

export interface Config {
  appEnv: string;
  production: boolean;
  publicBaseUrl: string;
  databaseUrl: string;
  redisRestUrl: string;
  redisRestToken: string;
  queuePrefix: string;
  adminToken: string;
  cronToken: string;
  metricsToken: string;
  metricsPublic: boolean;
  groqApiKey: string;
  geminiApiKey: string;
  groqModel: string;
  geminiModel: string;
  groqBaseUrl: string;
  geminiBaseUrl: string;
  providerOrder: string[];
  llmTimeoutMs: number;
  llmMaxRetries: number;
  llmMaxRounds: number;
  llmDefaultCooldownS: number;
  jobMaxAttempts: number;
  jobRetryBaseS: number;
  jobTimeoutMs: number;
  logRetentionDays: number;
  maxBodyBytes: number;
  subrequestBudget: number;
}

const str = (v: string | undefined, d = "") => (v ?? "").trim() || d;
const num = (v: string | undefined, d: number) => {
  const n = Number((v ?? "").trim());
  return (v ?? "").trim() === "" || Number.isNaN(n) ? d : n;
};
const bool = (v: string | undefined, d: boolean) => {
  const s = (v ?? "").trim().toLowerCase();
  return s === "" ? d : ["1", "true", "yes", "on"].includes(s);
};

/** rediss://default:TOKEN@HOST:6379 -> { url: https://HOST, token: TOKEN } (Upstash REST shares the password). */
export function upstashRestFromUrl(redisUrl: string): { url: string; token: string } | null {
  const m = /^rediss?:\/\/[^:@/]*:([^@]+)@([^:/]+)(?::\d+)?\/?$/.exec(redisUrl.trim());
  if (!m || !m[2].endsWith(".upstash.io")) return null;
  return { url: `https://${m[2]}`, token: decodeURIComponent(m[1]) };
}

export function loadConfig(env: Env): Config {
  const appEnv = str(env.APP_ENV, "production");
  let restUrl = str(env.UPSTASH_REDIS_REST_URL);
  let restToken = str(env.UPSTASH_REDIS_REST_TOKEN);
  if (!(restUrl && restToken) && env.UPSTASH_REDIS_URL) {
    const derived = upstashRestFromUrl(env.UPSTASH_REDIS_URL);
    if (derived) ({ url: restUrl, token: restToken } = derived);
  }
  const adminToken = str(env.ADMIN_TOKEN);
  return {
    appEnv,
    production: appEnv === "production",
    publicBaseUrl: str(env.PUBLIC_BASE_URL).replace(/\/+$/, ""),
    databaseUrl: str(env.DATABASE_URL),
    redisRestUrl: restUrl.replace(/\/+$/, ""),
    redisRestToken: restToken,
    queuePrefix: str(env.QUEUE_PREFIX, "ap"),
    adminToken,
    cronToken: str(env.CRON_TOKEN) || adminToken,
    metricsToken: str(env.METRICS_TOKEN),
    metricsPublic: bool(env.METRICS_PUBLIC, false),
    groqApiKey: str(env.GROQ_API_KEY),
    geminiApiKey: str(env.GEMINI_API_KEY),
    groqModel: str(env.GROQ_MODEL, "qwen/qwen3.8-27b"),
    geminiModel: str(env.GEMINI_MODEL, "gemini-flash-lite-latest"),
    groqBaseUrl: str(env.GROQ_BASE_URL, "https://api.groq.com/openai/v1"),
    geminiBaseUrl: str(env.GEMINI_BASE_URL, "https://generativelanguage.googleapis.com/v1beta/openai"),
    providerOrder: str(env.LLM_PROVIDER_ORDER, "groq,gemini").split(",").map((s) => s.trim()).filter(Boolean),
    llmTimeoutMs: num(env.LLM_TIMEOUT_SECONDS, 20) * 1000,
    llmMaxRetries: num(env.LLM_MAX_RETRIES, 1),
    llmMaxRounds: num(env.LLM_MAX_ROUNDS, 2),
    llmDefaultCooldownS: num(env.LLM_DEFAULT_COOLDOWN, 30),
    jobMaxAttempts: num(env.JOB_MAX_ATTEMPTS, 3),
    jobRetryBaseS: num(env.JOB_RETRY_BASE_SECONDS, 30),
    jobTimeoutMs: num(env.JOB_TIMEOUT_SECONDS, 25) * 1000,
    logRetentionDays: num(env.LOG_RETENTION_DAYS, 14),
    maxBodyBytes: num(env.MAX_BODY_BYTES, 1_000_000),
    // Workers Free allows 50 subrequests per invocation; keep a margin.
    subrequestBudget: num(env.SUBREQUEST_BUDGET, 45),
  };
}

/** Missing/odd settings, surfaced by /healthz so a misconfigured deploy can't look healthy. */
export function configWarnings(cfg: Config): string[] {
  const w: string[] = [];
  if (!cfg.databaseUrl) w.push("DATABASE_URL is not set");
  if (!(cfg.redisRestUrl && cfg.redisRestToken)) w.push("UPSTASH_REDIS_URL (or UPSTASH_REDIS_REST_URL + _TOKEN) is not set");
  if (!cfg.groqApiKey && !cfg.geminiApiKey) w.push("no LLM key set (GROQ_API_KEY / GEMINI_API_KEY)");
  else if (!cfg.groqApiKey || !cfg.geminiApiKey) w.push("only one LLM provider configured: no failover");
  if (!cfg.adminToken) w.push("ADMIN_TOKEN is not set (admin API disabled)");
  return w;
}
