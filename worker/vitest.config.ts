import { readFileSync } from "node:fs";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // same as wrangler's [[rules]] Text for *.html: import the demo page as a string
  plugins: [{
    name: "html-as-text",
    enforce: "pre",
    load(id) {
      if (id.endsWith(".html")) return `export default ${JSON.stringify(readFileSync(id, "utf8"))};`;
    },
  }],
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
    testTimeout: 20_000,
    fileParallelism: false, // integration tests share one Postgres/Redis
  },
});
