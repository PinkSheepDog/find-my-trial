import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import Handoff, { buildSummary } from "../src/components/Handoff.jsx";
import { DEFAULT_FILTERS } from "../src/lib/filters.js";

const HEALTH = {
  trial_count: 4321,
  degraded_mode: true,
  data_current_through: "2026-05-01",
  normalization_version: "1.1.0-disease-purpose-gates",
  app_version: "2.3.0",
  corpus_content_hash: "sha256:abc123",
  index_built_at: "2026-05-02T09:00:00Z",
  corpus_integrity_verified: true,
  deid_review_enforced: true,
};

const PROFILE = {
  extractor: "rules",
  summary_line: "62 F, metastatic HER2-positive breast cancer",
  biomarkers: [
    { name: "HER2", status: "positive", detail: "IHC 3+", method: "IHC", specimen: "liver core biopsy", date: "2026-04-02", timing: "current" },
    { name: "BRCA", status: "negative", timing: "current" },
  ],
};

const TRIAL = {
  rank: 1, nct: "NCT00000001", title: "A HER2 study", url: "https://example.org/1",
  status: "RECRUITING", phase: "Phase 2", match_score: 81.2, fit_label: "Strong fit",
  reasons: ["HER2-positive matches target"], cautions: ["Confirm ECOG"],
  contraindications: [], explained_by: "rules",
};

const NOW = new Date("2026-07-10T12:00:00Z");

describe("export provenance", () => {
  const summary = buildSummary(PROFILE, [TRIAL], { health: HEALTH, filters: DEFAULT_FILTERS, now: NOW });

  it("records when the export was generated", () => {
    expect(summary).toContain("Generated: 2026-07-10T12:00:00.000Z");
  });

  it("records the corpus date, size, age and index version", () => {
    expect(summary).toContain("Trial corpus current through: 2026-05-01");
    expect(summary).toContain("Corpus age at export: 70 day(s)");
    expect(summary).toContain("Trials indexed: 4,321");
    expect(summary).toContain("Index/normalization version: 1.1.0-disease-purpose-gates");
    expect(summary).toContain("Index built at: 2026-05-02T09:00:00Z");
    expect(summary).toContain("Corpus content hash: sha256:abc123");
  });

  it("records extractor and explanation provenance", () => {
    expect(summary).toContain("Profile extractor: rules");
    expect(summary).toContain("Explanation source: rules (degraded mode: no LLM key)");
    expect(summary).toContain("Server version: 2.3.0");
    expect(summary).toMatch(/Workspace build: /);
  });

  it("records the filters that shaped the list", () => {
    expect(summary).toContain("treatment_only=true");
    expect(summary).toContain("interventional_only=true");
  });

  it("warns when the corpus is old enough to have moved on", () => {
    expect(summary).toContain("Corpus may be stale");
  });

  it("says provenance is unverified rather than implying a check happened", () => {
    const unverified = buildSummary(PROFILE, [TRIAL], {
      health: { ...HEALTH, corpus_integrity_verified: false }, filters: DEFAULT_FILTERS, now: NOW,
    });
    expect(unverified).toContain("Corpus integrity: UNVERIFIED");
  });

  it("flags an export produced with the egress gate disabled", () => {
    const ungated = buildSummary(PROFILE, [TRIAL], {
      health: { ...HEALTH, deid_review_enforced: false }, filters: DEFAULT_FILTERS, now: NOW,
    });
    expect(ungated).toContain("review gate was DISABLED");
  });

  it("admits an unknown corpus date instead of leaving it blank", () => {
    const unknown = buildSummary(PROFILE, [TRIAL], { health: {}, filters: DEFAULT_FILTERS, now: NOW });
    expect(unknown).toContain("Trial corpus current through: UNKNOWN");
  });

  it("still builds when /health never resolved", () => {
    expect(() => buildSummary(PROFILE, [TRIAL], { now: NOW })).not.toThrow();
  });
});

describe("export content", () => {
  it("carries biomarker provenance, including a missing date", () => {
    const summary = buildSummary(PROFILE, [TRIAL], { health: HEALTH, filters: DEFAULT_FILTERS, now: NOW });
    expect(summary).toContain("HER2 positive, IHC 3+, IHC, liver core biopsy, dated 2026-04-02");
    expect(summary).toContain("BRCA negative, date not recorded");
  });

  it("carries evidence snippets from structured reasons", () => {
    const structured = {
      ...TRIAL,
      reasons: [{ text: "HER2-positive matches target", evidence_snippet: "HER2 IHC 3+", source_field: "biomarkers" }],
      cautions: [],
    };
    const summary = buildSummary(PROFILE, [structured], { health: HEALTH, filters: DEFAULT_FILTERS, now: NOW });
    expect(summary).toContain('HER2-positive matches target [evidence · biomarkers: "HER2 IHC 3+"]');
  });

  it("handles plain-string reasons unchanged", () => {
    const summary = buildSummary(PROFILE, [TRIAL], { health: HEALTH, filters: DEFAULT_FILTERS, now: NOW });
    expect(summary).toContain("Reasons: HER2-positive matches target");
    expect(summary).toContain("Manual checks: Confirm ECOG");
  });
});

describe("handoff panel", () => {
  it("shows the provenance-bearing summary once a trial is shortlisted", () => {
    render(
      <Handoff profile={PROFILE} results={[TRIAL]} shortlist={[TRIAL.nct]} health={HEALTH} filters={DEFAULT_FILTERS} />
    );
    const box = screen.getByLabelText(/handoff summary text/i);
    expect(box.value).toContain("PROVENANCE:");
    expect(box.value).toContain("Trial corpus current through:");
  });

  it("keeps the non-dismissible eligibility warning", () => {
    render(<Handoff profile={PROFILE} results={[TRIAL]} shortlist={[]} health={HEALTH} filters={DEFAULT_FILTERS} />);
    expect(screen.getByText(/not an eligibility determination/i)).toBeInTheDocument();
  });
});
