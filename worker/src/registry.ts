// Full handler registry (kept separate from handlers.ts to avoid import cycles).
import { deliver } from "./delivery";
import { BASE_HANDLERS, type Handler, INTERNAL_PREFIX, SYSTEM_PREFIX } from "./handlers";
import { PRODUCT_HANDLERS } from "./products";

export const HANDLERS: Record<string, Handler> = { ...BASE_HANDLERS, ...PRODUCT_HANDLERS, "internal.deliver": deliver };

export { INTERNAL_PREFIX, SYSTEM_PREFIX };

export const clientJobTypes = () =>
  Object.keys(HANDLERS).filter((n) => !n.startsWith(SYSTEM_PREFIX) && !n.startsWith(INTERNAL_PREFIX)).sort();

export const isClientCallable = (name: string) => name in HANDLERS && !name.startsWith(SYSTEM_PREFIX) && !name.startsWith(INTERNAL_PREFIX);
