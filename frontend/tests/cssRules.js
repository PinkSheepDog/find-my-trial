import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const STYLES_PATH = path.join(HERE, "..", "src", "styles.css");

export function readStyles() {
  return fs.readFileSync(STYLES_PATH, "utf8");
}

/**
 * Minimal CSS reader: returns a flat list of
 * `{ selector, media, declarations: [{ prop, value }] }`.
 * Enough for structural assertions; it is not a full CSS parser.
 */
export function parseCss(cssInput) {
  const css = cssInput.replace(/\/\*[\s\S]*?\*\//g, "");
  const rules = [];
  let i = 0;

  function parseDecls(body) {
    return body
      .split(";")
      .map((d) => d.trim())
      .filter(Boolean)
      .map((d) => {
        const idx = d.indexOf(":");
        if (idx === -1) return null;
        return { prop: d.slice(0, idx).trim().toLowerCase(), value: d.slice(idx + 1).trim() };
      })
      .filter(Boolean);
  }

  function parseAt(media) {
    let buf = "";
    while (i < css.length) {
      const ch = css[i];
      if (ch === "}") {
        i += 1;
        return;
      }
      if (ch === "{") {
        const head = buf.trim();
        buf = "";
        i += 1;
        if (head.startsWith("@media")) {
          parseAt(head.replace(/^@media\s*/, "").trim());
        } else if (head.startsWith("@")) {
          parseAt(media); // keyframes and friends: bodies are not width rules
        } else {
          let body = "";
          while (i < css.length && css[i] !== "}") {
            body += css[i];
            i += 1;
          }
          i += 1;
          head.split(",").forEach((sel) => {
            rules.push({ selector: sel.trim(), media, declarations: parseDecls(body) });
          });
        }
      } else {
        buf += ch;
        i += 1;
      }
    }
  }

  parseAt("");
  return rules;
}

/** Does a media condition apply at viewport width `w`? Non-width features pass. */
export function mediaApplies(media, w) {
  if (!media) return true;
  let ok = true;
  const maxRe = /\(\s*max-width:\s*(\d+)px\s*\)/g;
  const minRe = /\(\s*min-width:\s*(\d+)px\s*\)/g;
  let m;
  while ((m = maxRe.exec(media))) ok = ok && w <= Number(m[1]);
  while ((m = minRe.exec(media))) ok = ok && w >= Number(m[1]);
  return ok;
}

/**
 * Effective declarations at width `w`: later applicable rules win for the same
 * (selector, property) pair. An approximation of the cascade, ignoring
 * specificity — good enough to catch a fixed width that is never overridden.
 */
export function effectiveDeclarations(rules, w) {
  const out = new Map();
  rules.forEach((rule) => {
    if (!mediaApplies(rule.media, w)) return;
    rule.declarations.forEach(({ prop, value }) => {
      out.set(`${rule.selector}|${prop}`, { selector: rule.selector, prop, value, media: rule.media });
    });
  });
  return Array.from(out.values());
}

const PURE_PX = /^(\d+(?:\.\d+)?)px$/;

/** Pure `NNNpx` value, or null when the value is fluid (%, vw, min(), calc(), …). */
export function purePx(value) {
  const m = PURE_PX.exec(value.trim());
  return m ? Number(m[1]) : null;
}

/** Fixed px track sizes in a grid-template-columns value. */
export function gridPxTracks(value) {
  const tracks = [];
  // minmax(<min>, …) — only the minimum can force overflow.
  const minmaxRe = /minmax\(\s*([^,()]+|min\([^)]*\)|max\([^)]*\)|calc\([^)]*\))\s*,/g;
  let m;
  while ((m = minmaxRe.exec(value))) {
    const px = purePx(m[1]);
    if (px != null) tracks.push({ kind: "minmax-min", px });
  }
  // Bare px tracks outside any function call.
  const stripped = value.replace(/[a-z-]+\([^()]*(?:\([^()]*\)[^()]*)*\)/gi, " ");
  const bareRe = /(\d+(?:\.\d+)?)px/g;
  while ((m = bareRe.exec(stripped))) tracks.push({ kind: "fixed", px: Number(m[1]) });
  return tracks;
}
