import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ACTIVE_STATUSES, DEFAULT_FILTERS, RECRUITING_STATUSES, payloadForServer } from "../src/lib/filters.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCHEMAS = path.join(HERE, "..", "..", "backend", "app", "api", "schemas.py");

// The client used to send `interventional_only: false` against a server default
// of `true`, and to omit `treatment_only` entirely — so behaviour depended on
// which side set the field. This pins the two together.
describe("client defaults match the server contract", () => {
  const source = fs.existsSync(SCHEMAS) ? fs.readFileSync(SCHEMAS, "utf8") : null;

  function serverDefault(field) {
    const re = new RegExp(`^\\s*${field}\\s*:\\s*bool\\s*=\\s*(True|False)`, "m");
    const m = source && re.exec(source);
    return m ? m[1] === "True" : undefined;
  }

  it.runIf(source)("agrees with MatchRequest on every boolean filter", () => {
    ["active_only", "recruiting_only", "interventional_only", "treatment_only", "location_required"].forEach((field) => {
      const expected = serverDefault(field);
      if (expected === undefined) return; // field not modelled by this server build
      expect(DEFAULT_FILTERS[field], `${field} default differs from schemas.py`).toBe(expected);
    });
  });

  it.runIf(source)("agrees with MatchRequest on top_k", () => {
    const m = /top_k:\s*int\s*=\s*Field\(default=(\d+)/.exec(source);
    if (m) expect(DEFAULT_FILTERS.top_k).toBe(Number(m[1]));
  });

  it("states a value for every filter the UI exposes", () => {
    ["top_k", "active_only", "interventional_only", "treatment_only", "location"].forEach((f) => {
      expect(DEFAULT_FILTERS).toHaveProperty(f);
    });
  });
});

describe("capability-aware payloads", () => {
  it("sends everything when server capabilities are unknown", () => {
    expect(payloadForServer(DEFAULT_FILTERS, null)).toEqual(DEFAULT_FILTERS);
  });

  it("drops filters the server does not model", () => {
    const supported = new Set(["top_k", "active_only", "interventional_only", "treatment_only", "location"]);
    const payload = payloadForServer(DEFAULT_FILTERS, supported);
    expect(payload).not.toHaveProperty("recruiting_only");
    expect(payload).not.toHaveProperty("location_required");
    expect(payload).toHaveProperty("treatment_only", true);
  });

  it("includes recruiting_only once the server models it", () => {
    const supported = new Set([...Object.keys(DEFAULT_FILTERS)]);
    expect(payloadForServer({ ...DEFAULT_FILTERS, recruiting_only: true }, supported).recruiting_only).toBe(true);
  });
});

describe("status vocabularies are stated, not implied", () => {
  it("includes ACTIVE_NOT_RECRUITING in the active set", () => {
    expect(ACTIVE_STATUSES).toContain("ACTIVE_NOT_RECRUITING");
  });

  it("excludes ACTIVE_NOT_RECRUITING from the recruiting set", () => {
    expect(RECRUITING_STATUSES).not.toContain("ACTIVE_NOT_RECRUITING");
  });

  it("keeps the recruiting set a strict subset of the active set", () => {
    RECRUITING_STATUSES.forEach((s) => expect(ACTIVE_STATUSES).toContain(s));
    expect(RECRUITING_STATUSES.length).toBeLessThan(ACTIVE_STATUSES.length);
  });
});
