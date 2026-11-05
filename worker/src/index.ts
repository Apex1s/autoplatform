// Cloudflare Workers entry point (free plan): HTTP API + every-minute Cron Trigger.
import { handle } from "./api";
import { makeDeps } from "./deps";
import type { Env } from "./env";
import { runCron } from "./runner";

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    return handle(request, makeDeps(env), ctx);
  },
  async scheduled(event: ScheduledController, env: Env, _ctx: ExecutionContext): Promise<void> {
    const summary = await runCron(makeDeps(env), event.scheduledTime);
    console.log(JSON.stringify({ cron: summary }));
  },
} satisfies ExportedHandler<Env>;
