// The demo page is bundled as text (wrangler [[rules]] Text for *.html; a vitest plugin does the same in tests).
declare module "*.html" {
  const content: string;
  export default content;
}
