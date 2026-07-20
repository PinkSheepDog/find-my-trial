import { describe, expect, it } from "vitest";
import {
  effectiveDeclarations,
  gridPxTracks,
  parseCss,
  purePx,
  readStyles,
} from "./cssRules.js";

// Common narrow viewports the requirements call out.
const VIEWPORTS = [320, 375, 390, 430];

const rules = parseCss(readStyles());

describe("no horizontal overflow at 320–430px", () => {
  VIEWPORTS.forEach((w) => {
    describe(`${w}px`, () => {
      const decls = effectiveDeclarations(rules, w);

      it("declares no fixed width wider than the viewport", () => {
        const offenders = decls
          .filter((d) => d.prop === "width")
          .map((d) => ({ ...d, px: purePx(d.value) }))
          .filter((d) => d.px != null && d.px > w);
        expect(
          offenders,
          `fixed widths wider than ${w}px: ${JSON.stringify(offenders)}`
        ).toEqual([]);
      });

      it("declares no min-width wider than the viewport", () => {
        const offenders = decls
          .filter((d) => d.prop === "min-width")
          .map((d) => ({ ...d, px: purePx(d.value) }))
          .filter((d) => d.px != null && d.px > w);
        expect(
          offenders,
          `min-widths wider than ${w}px: ${JSON.stringify(offenders)}`
        ).toEqual([]);
      });

      it("declares no grid track that cannot shrink below the viewport", () => {
        const offenders = [];
        decls
          .filter((d) => d.prop === "grid-template-columns")
          .forEach((d) => {
            const tracks = gridPxTracks(d.value);
            const total = tracks.reduce((sum, t) => sum + t.px, 0);
            const tooWide = tracks.find((t) => t.px > w);
            // A `minmax(200px, 1fr)` floor larger than the container overflows —
            // `minmax(min(200px, 100%), 1fr)` is the fluid form.
            if (tooWide || total > w) offenders.push({ ...d, tracks });
          });
        expect(
          offenders,
          `rigid grid tracks at ${w}px: ${JSON.stringify(offenders)}`
        ).toEqual([]);
      });
    });
  });
});

// A green regression test that cannot go red is worthless: prove the analyzer
// still catches the exact defects this suite exists to prevent.
describe("the analyzer catches known-bad CSS", () => {
  it("flags a fixed-width card narrower viewports cannot hold", () => {
    const bad = parseCss(".login-card { width: 360px; }");
    const offenders = effectiveDeclarations(bad, 320)
      .filter((d) => d.prop === "width")
      .map((d) => purePx(d.value))
      .filter((px) => px != null && px > 320);
    expect(offenders).toEqual([360]);
  });

  it("flags a rigid minmax grid floor", () => {
    const tracks = gridPxTracks("repeat(auto-fit, minmax(200px, 1fr))");
    expect(tracks.some((t) => t.px === 200)).toBe(true);
    // The fluid form must NOT be flagged.
    expect(gridPxTracks("repeat(auto-fit, minmax(min(200px, 100%), 1fr))")).toEqual([]);
  });

  it("flags overflow-x masking on body", () => {
    const bad = parseCss("@media (max-width: 760px) { html, body { overflow-x: hidden; } }");
    const masks = bad.filter(
      (r) => /^(html|body)$/i.test(r.selector) &&
        r.declarations.some((d) => d.prop === "overflow-x" && /hidden|clip/.test(d.value))
    );
    expect(masks.length).toBe(2);
  });
});

describe("overflow is not masked", () => {
  it("does not hide horizontal overflow on html/body", () => {
    // `overflow-x: hidden` here would conceal a real layout bug (a 360px card on
    // a 320px screen) rather than fix it, and would defeat these tests.
    const masks = rules.filter(
      (r) =>
        /^(html|body)$/i.test(r.selector) &&
        r.declarations.some(
          (d) => d.prop === "overflow-x" && /hidden|clip/.test(d.value)
        )
    );
    expect(masks).toEqual([]);
  });

  it("keeps the login card fluid", () => {
    const decls = effectiveDeclarations(rules, 320).filter((d) => d.selector === ".login-card");
    const width = decls.find((d) => d.prop === "width");
    const maxWidth = decls.find((d) => d.prop === "max-width");
    expect(width?.value).toBe("100%");
    expect(maxWidth).toBeTruthy();
  });

  it("lets long unbroken content wrap", () => {
    const wrappers = rules.filter((r) =>
      r.declarations.some((d) => d.prop === "overflow-wrap" && /anywhere|break-word/.test(d.value))
    );
    expect(wrappers.length).toBeGreaterThan(0);
  });
});
