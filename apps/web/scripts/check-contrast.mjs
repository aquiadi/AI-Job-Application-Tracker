#!/usr/bin/env node
/**
 * Enforce WCAG AA contrast on the design tokens.
 *
 * "AA contrast" is easy to claim and easy to break — one darkening of a muted grey
 * to make a table look calmer and half the secondary text fails. This reads
 * src/styles/tokens.css, resolves both themes, and checks every pairing the app
 * actually renders. It runs as part of `npm run check`, so the claim is a test.
 *
 * Thresholds are WCAG 2.1: 4.5:1 for body text (1.4.3) and 3:1 for the boundary of a
 * user interface component (1.4.11 Non-text Contrast).
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const TOKENS = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "styles", "tokens.css");

const AA_TEXT = 4.5;
const AA_NON_TEXT = 3.0;

/**
 * Every pairing the interface puts on screen, with the rule that applies to it.
 * Adding a colour to the app means adding its pairing here.
 */
const PAIRINGS = [
  ["ink", "paper", AA_TEXT, "body text on the page"],
  ["ink", "surface", AA_TEXT, "body text on a panel"],
  ["ink", "surface-sunken", AA_TEXT, "body text on a sunken well"],
  ["ink-muted", "paper", AA_TEXT, "secondary text on the page"],
  ["ink-muted", "surface", AA_TEXT, "secondary text on a panel"],
  ["ink-faint", "paper", AA_TEXT, "timestamps and counts"],
  ["ink-faint", "surface", AA_TEXT, "timestamps and counts on a panel"],
  ["accent", "paper", AA_TEXT, "links on the page"],
  ["accent", "surface", AA_TEXT, "links on a panel"],
  ["accent-strong", "surface", AA_TEXT, "link hover"],
  ["on-accent", "accent", AA_TEXT, "text on a filled button"],
  ["alert", "paper", AA_TEXT, "error text"],
  ["alert", "surface", AA_TEXT, "error text on a panel"],
  ["on-alert", "alert", AA_TEXT, "text on a destructive button"],
  ["ink", "accent-wash", AA_TEXT, "text on a partial-evidence chip"],
  ["ink", "alert-wash", AA_TEXT, "text inside an error callout"],
  ["accent", "accent-wash", AA_NON_TEXT, "partial-evidence chip border"],
  ["edge", "paper", AA_NON_TEXT, "missing-evidence chip border"],
  ["edge", "surface", AA_NON_TEXT, "input and control borders"],
  ["edge", "surface-sunken", AA_NON_TEXT, "control borders in a sunken well"],
  ["accent", "paper", AA_NON_TEXT, "focus ring against the page"],
  ["accent", "surface", AA_NON_TEXT, "focus ring against a panel"],
];

/** Read the `:root` block and the dark-scheme override out of the token file. */
function parseThemes(css) {
  const light = {};
  const dark = {};

  const rootBlock = css.match(/:root\s*\{([\s\S]*?)\n\}/);
  if (!rootBlock) throw new Error("tokens.css: no :root block found");
  collect(rootBlock[1], light);

  const darkBlock = css.match(/prefers-color-scheme:\s*dark[\s\S]*?:root\s*\{([\s\S]*?)\n\s*\}/);
  if (!darkBlock) throw new Error("tokens.css: no dark-scheme :root override found");
  Object.assign(dark, light);
  collect(darkBlock[1], dark);

  return { light, dark };
}

function collect(block, into) {
  for (const line of block.split("\n")) {
    const match = line.match(/^\s*--([a-z0-9-]+)\s*:\s*([^;]+);/i);
    if (match) into[match[1]] = match[2].trim();
  }
}

/** Follow `var(--x)` chains until a literal colour falls out. */
function resolve(theme, name, seen = new Set()) {
  if (seen.has(name)) throw new Error(`tokens.css: --${name} is defined in terms of itself`);
  seen.add(name);

  const value = theme[name];
  if (value === undefined) throw new Error(`tokens.css: --${name} is not defined`);

  const indirect = value.match(/^var\(\s*--([a-z0-9-]+)\s*\)$/i);
  return indirect ? resolve(theme, indirect[1], seen) : value;
}

function toRgb(value) {
  const hex = value.trim();
  if (!/^#[0-9a-f]{6}$/i.test(hex)) throw new Error(`not a 6-digit hex colour: ${value}`);
  const n = Number.parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function relativeLuminance(rgb) {
  const [r, g, b] = rgb.map((channel) => {
    const c = channel / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const [hi, lo] = [relativeLuminance(toRgb(a)), relativeLuminance(toRgb(b))].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const themes = parseThemes(readFileSync(TOKENS, "utf8"));
const failures = [];
const rows = [];

for (const [themeName, theme] of Object.entries(themes)) {
  for (const [fg, bg, minimum, usage] of PAIRINGS) {
    const ratio = contrast(resolve(theme, fg), resolve(theme, bg));
    const passed = ratio >= minimum;
    rows.push({ themeName, fg, bg, ratio, minimum, usage, passed });
    if (!passed) failures.push({ themeName, fg, bg, ratio, minimum, usage });
  }
}

const width = Math.max(...rows.map((r) => `${r.fg} on ${r.bg}`.length));
for (const row of rows) {
  const pair = `${row.fg} on ${row.bg}`.padEnd(width);
  const mark = row.passed ? "pass" : "FAIL";
  process.stdout.write(
    `${row.themeName.padEnd(5)} ${pair}  ${row.ratio.toFixed(2).padStart(6)}:1  ` +
      `min ${row.minimum.toFixed(1)}  ${mark}  ${row.usage}\n`,
  );
}

if (failures.length > 0) {
  process.stderr.write(`\n${failures.length} pairing(s) below WCAG AA:\n`);
  for (const f of failures) {
    process.stderr.write(
      `  ${f.themeName}: --${f.fg} on --${f.bg} is ${f.ratio.toFixed(2)}:1, ` +
        `needs ${f.minimum.toFixed(1)}:1 (${f.usage})\n`,
    );
  }
  process.exit(1);
}

process.stdout.write(`\n${rows.length} pairings checked, all at or above WCAG AA.\n`);
