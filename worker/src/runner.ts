// Job execution on a serverless runtime: no resident worker. Jobs run
//   (a) right after the webhook response (ctx.waitUntil), and
//   (b) from the every-minute Cron Trigger, which also promotes delayed retries,
//       runs maintenance at :17/:47 and probes providers every 6 hours.
// Claiming is the same atomic UPDATE as the Python worker, so delivery is at least once.
import type { Deps } from "./deps";
import type { JobCtx } from "./handlers";
import { enqueueWeeklyReports } from "./products";
import { HANDLERS } from "./registry";
import { attemptsToUsage, AllProvidersFailed } from "./llm";
import * as repo from "./repo";
import { BudgetExceeded, PermanentError } from "./util";

const RESERVE_PER_JOB = 10; // subrequests a job may need (claim, LLM tries, finish, retry enqueue)

class JobTimeout extends Error {}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  let t: ReturnType<typeof setTimeout>;
  return Promise.race([p, new Promise<T>((_, rej) => (t = setTimeout(() => rej(new JobTimeout(`job exceeded ${ms}ms`)), ms)))])
    .finally(() => clearTimeout(t));
}

export type Outcome = "skipped" | "succeeded" | "retrying" | "failed";

export async function processJob(deps: Deps, jobId: string): Promise<Outcome> {
  const { db, queue, cfg } = deps;
  const job = await repo.claimJob(db, jobId);
  if (!job) return "skipped"; // duplicate id or already finished
  const ctx: JobCtx = { jobId, clientId: job.client_id, attempt: job.attempts, createdAt: job.created_at, deps, usage: [] };
  const started = Date.now();
  const fn = HANDLERS[job.type] ?? SYSTEM_HANDLERS[job.type];
  try {
    if (!fn) throw new PermanentError(`unknown job type '${job.type}'`);
    const result = await withTimeout(fn(ctx, job.payload ?? {}), cfg.jobTimeoutMs);
    await repo.finishJob(db, jobId, { status: repo.STATUS.SUCCEEDED, workerId: deps.workerId, attempt: job.attempts,
      durationMs: Date.now() - started, clientId: job.client_id, result, usage: ctx.usage });
    await queue.incr("processed").catch(() => undefined);
    return "succeeded";
  } catch (e) {
    if (e instanceof BudgetExceeded) throw e; // job stays 'running'; maintenance releases it after the lease
    const err = `${(e as Error).name}: ${(e as Error).message}`;
    const permanent = e instanceof PermanentError || job.attempts >= job.max_attempts;
    if (permanent) {
      await repo.finishJob(db, jobId, { status: repo.STATUS.FAILED, workerId: deps.workerId, attempt: job.attempts,
        durationMs: Date.now() - started, clientId: job.client_id, error: err, usage: ctx.usage });
      await queue.incr("failed").catch(() => undefined);
      return "failed";
    }
    const delay = Math.min(600, cfg.jobRetryBaseS * 2 ** (job.attempts - 1)) * (0.75 + Math.random() / 2);
    await repo.finishJob(db, jobId, { status: repo.STATUS.RETRYING, workerId: deps.workerId, attempt: job.attempts,
      durationMs: Date.now() - started, clientId: job.client_id, error: err, retryDelayS: delay, usage: ctx.usage });
    await queue.enqueue(jobId, delay);
    return "retrying";
  }
}

export async function drain(deps: Deps, o: { maxJobs: number; deadline: number }) {
  const out: Record<Outcome, number> = { skipped: 0, succeeded: 0, retrying: 0, failed: 0 };
  let n = 0;
  while (n < o.maxJobs && Date.now() < o.deadline && deps.budget.left >= RESERVE_PER_JOB + 1) {
    const id = await deps.queue.pop();
    if (!id) break;
    n++;
    try {
      out[await processJob(deps, id)]++;
    } catch (e) {
      if (e instanceof BudgetExceeded) break;
      console.error(`job ${id} crashed the runner`, e);
      await deps.queue.enqueue(id, 30).catch(() => undefined);
    }
  }
  return out;
}

export async function maintenance(deps: Deps) {
  const { db, queue, cfg } = deps;
  const stuck = await repo.stuckJobs(db, 10, Math.ceil((3 * cfg.jobTimeoutMs) / 1000));
  let requeued = 0;
  for (const id of stuck) {
    if (deps.budget.left < 8) break;
    await queue.enqueue(id); // duplicates are harmless: claimJob is atomic
    requeued++;
  }
  const promoted = await queue.promoteDue();
  const pruned = await repo.prune(db, cfg.logRetentionDays);
  const stats = await queue.stats();
  await repo.heartbeat(db, deps.workerId, "cloudflare", stats.processed ?? 0, stats.failed ?? 0);
  return { stuck: stuck.length, requeued, promoted, pruned, workers_snapshotted: 1 };
}

export async function providerProbe(deps: Deps) {
  const results: Record<string, unknown> = {};
  const usage: repo.UsageRecord[] = [];
  for (const name of deps.router.configured) {
    try {
      const r = await deps.router.complete([{ role: "user", content: "Reply with the single word OK." }], { maxTokens: 5, only: [name] });
      usage.push(...attemptsToUsage(r.attempts, null, null));
      results[name] = { ok: true, latency_ms: r.latency_ms, model: r.model };
    } catch (e) {
      if (e instanceof AllProvidersFailed) usage.push(...attemptsToUsage((e as any).attempts ?? [], null, null));
      results[name] = { ok: false, error: String((e as Error).message).slice(0, 300) };
    }
  }
  await repo.recordUsage(deps.db, usage);
  return { providers: results };
}

// System jobs (enqueued via POST /cron/:name, like the Python app)
const SYSTEM_HANDLERS: Record<string, (ctx: JobCtx, payload: any) => Promise<unknown>> = {
  "system.maintenance": (ctx) => maintenance(ctx.deps),
  "system.provider_probe": (ctx) => providerProbe(ctx.deps),
};

/** Every-minute Cron Trigger. */
export async function runCron(deps: Deps, scheduledTime: number) {
  const t = new Date(scheduledTime);
  const minute = t.getUTCMinutes(), hour = t.getUTCHours();
  const summary: Record<string, unknown> = { at: t.toISOString() };
  try {
    summary.promoted = await deps.queue.promoteDue();
    if (minute === 17 || minute === 47) summary.maintenance = await maintenance(deps);
    if (minute === 17 && hour % 6 === 0) summary.provider_probe = await providerProbe(deps);
    if (minute === 17 && hour === 8 && t.getUTCDay() === 1) summary.weekly_reports = (await enqueueWeeklyReports(deps)).length;
    summary.drain = await drain(deps, { maxJobs: 4, deadline: Date.now() + 25_000 });
  } catch (e) {
    summary.error = String((e as Error).message).slice(0, 300);
    console.error("cron run failed", e);
  } finally {
    summary.subrequests = deps.budget.used + 1;
    try {
      deps.budget.used = Math.min(deps.budget.used, deps.budget.max - 1); // always record liveness
      await deps.queue.markCronRun(summary);
    } catch (e) {
      console.error("could not record cron run", e);
    }
  }
  return summary;
}
