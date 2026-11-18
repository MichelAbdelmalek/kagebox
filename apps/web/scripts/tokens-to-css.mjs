// Generate the Tailwind v4 @theme block from docs/design/tokens.json.
// Usage: node scripts/tokens-to-css.mjs <tokens.json> <out.css> [--check]
// Components use these theme classes; hex values are never hard-coded in components.
import { readFileSync, writeFileSync, existsSync } from "node:fs";

const [src, out, flag] = process.argv.slice(2);
const t = JSON.parse(readFileSync(src, "utf8"));

// Self-hosted variable fonts register as "<Family> Variable"; put them first.
const VARIABLE = ["Space Grotesk", "Inter", "JetBrains Mono"];
const withVariable = (stack) => {
  let s = stack;
  for (const f of VARIABLE) {
    s = s.replace(new RegExp(`(^|,\\s*)'?${f}'?(?=\\s*,|$)`), `$1'${f} Variable', '${f}'`);
  }
  return s;
};

const lines = ["/* Generated from docs/design/tokens.json by scripts/tokens-to-css.mjs. Do not edit. */", "@theme {"];
for (const [k, v] of Object.entries(t.color)) lines.push(`  --color-${k}: ${v};`);
for (const [k, v] of Object.entries(t.font)) lines.push(`  --font-${k}: ${withVariable(v)};`);
for (const [k, v] of Object.entries(t.radius)) lines.push(`  --radius-${k}: ${v};`);
lines.push(`  --spacing-touch: ${t.size["touch-target-min"]};`);
lines.push(`  --spacing-nav: ${t.size["nav-width"]};`);
lines.push("}", "");
const css = lines.join("\n");

if (flag === "--check") {
  if (!existsSync(out) || readFileSync(out, "utf8") !== css) {
    console.error(`${out} is out of date; run pnpm tokens`);
    process.exit(1);
  }
} else {
  writeFileSync(out, css);
}
