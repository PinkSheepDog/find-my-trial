import React from "react";
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import TrialBoard, { resolvePurpose } from "../src/components/TrialBoard.jsx";
import { normalizeNote, normalizeNotes, noteToText } from "../src/lib/notes.js";

function trial(overrides = {}) {
  return {
    rank: 1,
    nct: "NCT00000001",
    title: "A study of something",
    url: "https://example.org/NCT00000001",
    status: "RECRUITING",
    phase: "Phase 2",
    study_type: "INTERVENTIONAL",
    sponsor: "Sponsor",
    brief_summary: "Summary.",
    conditions: [],
    interventions: [],
    locations: [],
    match_score: 72.4,
    fit_label: "Promising",
    reasons: [],
    cautions: [],
    contraindications: [],
    breakdown: { condition: 30, biomarker: 20, contraindication_penalty: -15 },
    eligibility_sex: "ALL",
    eligibility_age: "18 Years and older",
    disease_family: "breast",
    study_purpose: "treatment",
    explained_by: "rules",
    ...overrides,
  };
}

function board(results, extra = {}) {
  return {
    results,
    candidate_count: results.length,
    trial_count: 1000,
    semantic_used: false,
    degraded_mode: true,
    needs_review: false,
    review_reasons: [],
    ...extra,
  };
}

function renderBoard(results, extra) {
  return render(<TrialBoard match={board(results, extra)} shortlist={[]} onToggle={() => {}} />);
}

describe("score breakdown", () => {
  it("marks a negative contribution as a penalty rather than drawing it positive", () => {
    const { container } = renderBoard([trial()]);
    fireEvent.click(screen.getByRole("button", { name: /score breakdown/i }));

    const penaltyRow = container.querySelector(".bar-row.is-penalty");
    expect(penaltyRow).toBeTruthy();
    expect(penaltyRow.textContent).toContain("contraindication penalty");
    // The sign is explicit in the label...
    expect(penaltyRow.textContent).toContain("-15");
    // ...and the fill is visually distinct from a positive contribution.
    expect(penaltyRow.querySelector(".fill").className).toContain("fill-neg");
    expect(penaltyRow.querySelector(".bar").className).toContain("bar-neg");
  });

  it("does not mark positive contributions as penalties", () => {
    const { container } = renderBoard([trial()]);
    fireEvent.click(screen.getByRole("button", { name: /score breakdown/i }));

    const rows = Array.from(container.querySelectorAll(".bar-row"));
    const condition = rows.find((r) => r.textContent.includes("condition"));
    expect(condition.className).not.toContain("is-penalty");
    expect(condition.querySelector(".fill").className).not.toContain("fill-neg");
    expect(condition.textContent).toContain("+30");
  });

  it("sizes bars by magnitude without letting a penalty read as a contribution", () => {
    const { container } = renderBoard([trial()]);
    fireEvent.click(screen.getByRole("button", { name: /score breakdown/i }));
    const penalty = container.querySelector(".bar-row.is-penalty .fill");
    expect(penalty.style.width).toBe("15%");
  });

  it("survives a missing breakdown", () => {
    const { container } = renderBoard([trial({ breakdown: undefined })]);
    fireEvent.click(screen.getByRole("button", { name: /score breakdown/i }));
    expect(container.querySelectorAll(".bar-row")).toHaveLength(0);
    expect(container.querySelector(".meta-grid")).toBeTruthy();
  });
});

describe("study-purpose badges", () => {
  it("labels imaging studies distinctly", () => {
    expect(resolvePurpose({ study_purpose: "imaging" })).toBe("imaging");
    expect(
      resolvePurpose({ study_purpose: "diagnostic", title: "PET/CT imaging of HER2 lesions" })
    ).toBe("imaging");
    renderBoard([trial({ study_purpose: "imaging" })]);
    expect(screen.getByText("Imaging")).toBeInTheDocument();
  });

  it("labels registry studies distinctly", () => {
    expect(resolvePurpose({ study_purpose: "registry" })).toBe("registry");
    expect(
      resolvePurpose({ study_purpose: "observational", title: "A national breast cancer registry" })
    ).toBe("registry");
    renderBoard([trial({ study_purpose: "registry" })]);
    expect(screen.getByText("Registry")).toBeInTheDocument();
  });

  it("shows unknown purpose instead of suppressing the badge", () => {
    renderBoard([trial({ study_purpose: "unknown" })]);
    expect(screen.getByText("Purpose unknown")).toBeInTheDocument();
  });

  it("shows a purpose badge even when the field is absent", () => {
    renderBoard([trial({ study_purpose: undefined })]);
    expect(screen.getByText("Purpose unknown")).toBeInTheDocument();
  });

  it("keeps plain diagnostic and observational labels when nothing suggests otherwise", () => {
    expect(resolvePurpose({ study_purpose: "diagnostic", title: "Biopsy assay validation" })).toBe("diagnostic");
    expect(resolvePurpose({ study_purpose: "observational", title: "Cohort follow-up" })).toBe("observational");
  });
});

// The backend is migrating reasons/cautions from list[str] to structured entries
// carrying verbatim evidence. Both must render.
describe("reasons and cautions accept both response shapes", () => {
  it("renders plain strings", () => {
    renderBoard([trial({ reasons: ["HER2-positive matches the target"], cautions: ["Confirm ECOG"] })]);
    expect(screen.getByText("HER2-positive matches the target")).toBeInTheDocument();
    expect(screen.getByText("Confirm ECOG")).toBeInTheDocument();
  });

  it("renders structured entries with their evidence snippet", () => {
    renderBoard([
      trial({
        reasons: [
          {
            text: "HER2-positive matches the target",
            evidence_snippet: "HER2 IHC 3+ on liver biopsy",
            source_field: "biomarkers",
          },
        ],
        cautions: [{ text: "Confirm ECOG", evidence_snippet: "ECOG 1" }],
      }),
    ]);
    expect(screen.getByText("HER2-positive matches the target")).toBeInTheDocument();
    expect(screen.getByText(/HER2 IHC 3\+ on liver biopsy/)).toBeInTheDocument();
    expect(screen.getByText(/evidence · biomarkers/)).toBeInTheDocument();
    expect(screen.getByText(/ECOG 1/)).toBeInTheDocument();
  });

  it("renders a mixed list without dropping either shape", () => {
    renderBoard([
      trial({ reasons: ["Plain reason", { text: "Structured reason", evidence_snippet: "quote" }] }),
    ]);
    expect(screen.getByText("Plain reason")).toBeInTheDocument();
    expect(screen.getByText("Structured reason")).toBeInTheDocument();
  });

  it("renders structured contraindications", () => {
    const { container } = renderBoard([
      trial({
        contraindications: [
          { text: "Prior trastuzumab excluded", evidence_snippet: "no prior anti-HER2 therapy" },
        ],
      }),
    ]);
    expect(screen.getByText(/Prior trastuzumab excluded/)).toBeInTheDocument();
    expect(container.querySelector(".card-conflict")).toBeTruthy();
  });

  it("marks a claim the server says has no verbatim backing", () => {
    renderBoard([
      trial({ reasons: [{ text: "Likely a good fit", evidence_snippet: "", source_field: "", grounded: false }] }),
    ]);
    // Assert the behaviour, not the exact sentence: the claim is still shown, it is
    // labelled unverified, and it is not dressed up as a quote.
    expect(screen.getByText(/Likely a good fit/)).toBeInTheDocument();
    expect(screen.getByText(/unverified/i)).toBeInTheDocument();
    expect(screen.getByText(/verbatim trial text/i)).toBeInTheDocument();
    expect(document.querySelector(".note-evidence.ungrounded q")).toBeNull();
  });

  it("does not label plain strings as ungrounded", () => {
    // An older server sends no `grounded` flag; absence is not evidence of absence.
    renderBoard([trial({ reasons: ["A plain reason"] })]);
    expect(screen.queryByText(/no verbatim trial text backs this/i)).toBeNull();
  });

  it("prefers the evidence snippet over the ungrounded marker", () => {
    renderBoard([
      trial({ reasons: [{ text: "Matches", evidence_snippet: "HER2 required", source_field: "eligibility", grounded: true }] }),
    ]);
    expect(screen.getByText(/HER2 required/)).toBeInTheDocument();
    expect(screen.queryByText(/no verbatim trial text backs this/i)).toBeNull();
  });

  it("does not break when the fields are absent entirely", () => {
    renderBoard([trial({ reasons: undefined, cautions: undefined, contraindications: undefined })]);
    expect(screen.getByText("A study of something")).toBeInTheDocument();
  });
});

describe("note normalization", () => {
  it("accepts strings, objects, partials and junk", () => {
    expect(normalizeNote("hello")).toEqual({ text: "hello", evidence: "", source: "", grounded: true });
    expect(normalizeNote({ text: "a", evidence_snippet: "b", source_field: "c" })).toEqual({
      text: "a", evidence: "b", source: "c", grounded: true,
    });
    expect(normalizeNote({ text: "only text" })).toEqual({
      text: "only text", evidence: "", source: "", grounded: true,
    });
    // Evidence with no text still shows something rather than vanishing.
    expect(normalizeNote({ evidence_snippet: "quote" })).toEqual({
      text: "quote", evidence: "quote", source: "", grounded: true,
    });
    // `grounded` is only false when the server says so.
    expect(normalizeNote({ text: "a", grounded: false }).grounded).toBe(false);
    expect(normalizeNote(null)).toBeNull();
    expect(normalizeNote("")).toBeNull();
    expect(normalizeNote({})).toBeNull();
    expect(normalizeNotes(undefined)).toEqual([]);
    expect(normalizeNotes(["a", null, ""])).toEqual([
      { text: "a", evidence: "", source: "", grounded: true },
    ]);
  });

  it("flattens to text with evidence for exports", () => {
    expect(noteToText({ text: "a", evidence: "b", source: "c" })).toBe('a [evidence · c: "b"]');
    expect(noteToText({ text: "a", evidence: "", source: "" })).toBe("a");
  });
});

// Fields added by the landed ranking work. Each is optional: an older response
// omits them and must still render.
describe("gate caveats and location signals", () => {
  it("flags a trial whose disease area could not be classified", () => {
    renderBoard([trial({ disease_unclassified: true })]);
    expect(screen.getByText(/disease unclassified/i)).toBeInTheDocument();
  });

  it("marks an inferred study purpose as inferred", () => {
    renderBoard([trial({ study_purpose: "treatment", purpose_unverified: true })]);
    expect(screen.getByText(/\(inferred\)/i)).toBeInTheDocument();
  });

  it("does not add caveats when the flags are false or absent", () => {
    renderBoard([trial()]);
    expect(screen.queryByText(/disease unclassified/i)).toBeNull();
    expect(screen.queryByText(/\(inferred\)/i)).toBeNull();
  });

  it("shows a matching study site when one was found", () => {
    renderBoard([trial({ location_match: true, matched_locations: ["Detroit, Michigan", "Ann Arbor, Michigan"] })]);
    expect(screen.getByText(/site near you: Detroit, Michigan/)).toBeInTheDocument();
    expect(screen.getByText(/\+1/)).toBeInTheDocument();
  });

  it("surfaces the server's location notice", () => {
    renderBoard([trial()], {
      location_query: "Alaska",
      location_match_count: 0,
      location_notice: "No listed study site in Alaska among these results",
    });
    expect(screen.getByText(/No listed study site in Alaska/)).toBeInTheDocument();
    expect(screen.getByText(/0 with a site matching/)).toBeInTheDocument();
  });

  it("reports statements withheld for lacking evidence", () => {
    renderBoard([trial({ ungrounded_dropped: 3 })]);
    expect(screen.getByText(/3 generated statements were withheld/i)).toBeInTheDocument();
  });

  it("lists flattened source quotes, excluding unverified ones", () => {
    renderBoard([
      trial({
        evidence: [
          { text: "condition", evidence_snippet: "HER2-positive Breast Cancer", source_field: "conditions", grounded: true },
          { text: "bogus", evidence_snippet: "not in the record", source_field: "title", grounded: false },
        ],
      }),
    ]);
    fireEvent.click(screen.getByRole("button", { name: /score breakdown/i }));
    expect(screen.getByText(/HER2-positive Breast Cancer/)).toBeInTheDocument();
    expect(screen.queryByText(/not in the record/)).toBeNull();
  });

  it("treats a negative location contribution as a penalty, like the contraindication one", () => {
    const { container } = renderBoard([
      trial({ breakdown: { disease: 20, purpose: 5, location: -8, contraindication_penalty: 0 } }),
    ]);
    fireEvent.click(screen.getByRole("button", { name: /score breakdown/i }));

    const rows = Array.from(container.querySelectorAll(".bar-row"));
    const location = rows.find((r) => r.textContent.includes("location"));
    expect(location.className).toContain("is-penalty");
    expect(location.querySelector(".fill").className).toContain("fill-neg");
    expect(location.textContent).toContain("-8");
  });
});

describe("result counts are announced", () => {
  it("marks the count line as a live status", () => {
    const { container } = renderBoard([trial()]);
    expect(container.querySelector('.panel-head p[role="status"]')).toBeTruthy();
  });

  it("marks the empty state as a status", () => {
    const { container } = renderBoard([]);
    expect(container.querySelector('.empty[role="status"]')).toBeTruthy();
  });

  it("marks the abstention block as a status", () => {
    const { container } = render(
      <TrialBoard
        match={board([], { needs_review: true, review_reasons: ["No cancer type extracted"] })}
        shortlist={[]}
        onToggle={() => {}}
      />
    );
    expect(container.querySelector('.needs-review[role="status"]')).toBeTruthy();
    expect(screen.getByText("No cancer type extracted")).toBeInTheDocument();
  });
});
