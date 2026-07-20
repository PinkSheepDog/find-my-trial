import { describe, expect, it } from "vitest";
import { effectiveDeclarations, parseCss, purePx, readStyles } from "./cssRules.js";

const css = readStyles();
const rules = parseCss(css);

describe("focus visibility", () => {
  it("defines :focus-visible indicators", () => {
    const focusVisible = rules.filter((r) => r.selector.includes(":focus-visible"));
    expect(focusVisible.length).toBeGreaterThan(0);
  });

  it("gives every focus rule a visible outline", () => {
    const withOutline = rules.filter(
      (r) =>
        r.selector.includes(":focus") &&
        r.declarations.some((d) => d.prop === "outline" && !/none/.test(d.value))
    );
    expect(withOutline.length).toBeGreaterThan(0);
  });

  it("covers links, buttons and form controls", () => {
    const focusSelectors = rules
      .filter((r) => r.selector.includes(":focus"))
      .map((r) => r.selector)
      .join(" ");
    ["a:focus", "button:focus", "input:focus", "textarea:focus", "select:focus"].forEach((sel) => {
      expect(focusSelectors).toContain(sel);
    });
  });

  it("uses a distinct focus colour on the dark sidebar", () => {
    const sidebarFocus = rules.filter(
      (r) => r.selector.startsWith(".sidebar") && r.selector.includes(":focus")
    );
    expect(sidebarFocus.length).toBeGreaterThan(0);
    expect(
      sidebarFocus.some((r) =>
        r.declarations.some((d) => d.prop === "outline-color" || d.prop === "outline")
      )
    ).toBe(true);
  });
});

describe("touch targets", () => {
  // 44x44 is required in BOTH dimensions and at EVERY width — not min-height
  // alone inside a mobile-only media query.
  const TAP = 44;

  [320, 375, 430, 1280].forEach((w) => {
    it(`meets 44x44 for buttons and text-link buttons at ${w}px`, () => {
      const decls = effectiveDeclarations(rules, w);
      const targets = ["button", ".link", ".shortlist"];

      targets.forEach((sel) => {
        const covering = decls.filter((d) =>
          d.selector.split(/\s*,\s*/).some((s) => s === sel) ||
          d.selector === sel
        );
        const minH = covering.find((d) => d.prop === "min-height");
        const minW = covering.find((d) => d.prop === "min-width");
        expect(minH, `${sel} has no min-height at ${w}px`).toBeTruthy();
        expect(minW, `${sel} has no min-width at ${w}px`).toBeTruthy();
      });
    });
  });

  it("resolves the tap token to at least 44px", () => {
    const root = rules.find((r) => r.selector === ":root");
    const tap = root.declarations.find((d) => d.prop === "--tap");
    expect(purePx(tap.value)).toBeGreaterThanOrEqual(TAP);
  });
});

describe("navigation is never merely hidden", () => {
  it("never sets display:none on the nav", () => {
    const hiddenNav = rules.filter(
      (r) =>
        /\.nav\b/.test(r.selector) &&
        r.declarations.some((d) => d.prop === "display" && d.value.trim() === "none")
    );
    expect(
      hiddenNav,
      "the nav must become a drawer at narrow widths, not disappear"
    ).toEqual([]);
  });

  it("hides the closed drawer with visibility, keeping it animatable and toggleable", () => {
    const drawer = effectiveDeclarations(rules, 375).filter((d) => d.selector === ".sidebar");
    const visibility = drawer.find((d) => d.prop === "visibility");
    const transform = drawer.find((d) => d.prop === "transform");
    expect(visibility?.value).toBe("hidden");
    expect(transform?.value).toContain("translateX");

    const open = effectiveDeclarations(rules, 375).filter((d) => d.selector === ".sidebar.open");
    expect(open.find((d) => d.prop === "visibility")?.value).toBe("visible");
  });

  it("shows the hamburger bar only in drawer mode", () => {
    expect(effectiveDeclarations(rules, 1280).find((d) => d.selector === ".mobile-bar" && d.prop === "display")?.value)
      .toBe("none");
    expect(effectiveDeclarations(rules, 375).find((d) => d.selector === ".mobile-bar" && d.prop === "display")?.value)
      .toBe("flex");
  });
});

describe("screen-reader helpers", () => {
  it("provides an .sr-only utility", () => {
    const srOnly = rules.find((r) => r.selector === ".sr-only");
    expect(srOnly).toBeTruthy();
    // Must not use display:none — that removes it from the a11y tree too.
    expect(srOnly.declarations.some((d) => d.prop === "display" && d.value === "none")).toBe(false);
  });

  it("provides a skip link", () => {
    expect(rules.some((r) => r.selector === ".skip-link")).toBe(true);
    expect(rules.some((r) => r.selector === ".skip-link:focus")).toBe(true);
  });

  it("respects reduced-motion preferences", () => {
    expect(css).toContain("prefers-reduced-motion");
  });
});
