// Node module hook: import *.html as a text module (what wrangler's [[rules]] Text does on Cloudflare).
import { readFile } from "node:fs/promises";

export async function load(url, context, nextLoad) {
  if (url.endsWith(".html")) {
    const source = `export default ${JSON.stringify(await readFile(new URL(url), "utf8"))};`;
    return { format: "module", shortCircuit: true, source };
  }
  return nextLoad(url, context);
}
