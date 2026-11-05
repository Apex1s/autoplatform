// Usage: node --import ./scripts/register-html.mjs ...  (used by `npm run live`)
import { register } from "node:module";

register("./html-hooks.mjs", import.meta.url);
