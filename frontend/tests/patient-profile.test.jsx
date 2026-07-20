import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import PatientProfile from "../src/components/PatientProfile.jsx";

function profile(overrides = {}) {
  return {
    extractor: "rules",
    summary_line: "62 F, metastatic breast cancer",
    cancer_types: ["breast cancer"],
    biomarkers: [],
    therapies: [],
    facts: [],
    ...overrides,
  };
}

const FULL_MARKER = {
  name: "HER2",
  status: "positive",
  detail: "IHC 3+",
  specimen: "liver core biopsy",
  method: "IHC",
  date: "2026-04-02",
  timing: "current",
  certainty: "stated",
};

describe("biomarker provenance is visible, not hover-only", () => {
  it("renders disease, specimen, method and date as text", () => {
    const { container } = render(<PatientProfile profile={profile({ biomarkers: [FULL_MARKER] })} />);
    const card = container.querySelector(".bio-card");

    expect(within(card).getByText("HER2")).toBeInTheDocument();
    expect(within(card).getByText("positive")).toBeInTheDocument();
    expect(within(card).getByText("IHC 3+")).toBeInTheDocument();
    expect(within(card).getByText("Specimen")).toBeInTheDocument();
    expect(within(card).getByText("liver core biopsy")).toBeInTheDocument();
    expect(within(card).getByText("Method")).toBeInTheDocument();
    expect(within(card).getByText("IHC")).toBeInTheDocument();
    expect(within(card).getByText("Date")).toBeInTheDocument();
    expect(within(card).getByText("2026-04-02")).toBeInTheDocument();
    expect(within(card).getByText("Disease")).toBeInTheDocument();
  });

  it("does not hide provenance in a title tooltip", () => {
    const { container } = render(<PatientProfile profile={profile({ biomarkers: [FULL_MARKER] })} />);
    const card = container.querySelector(".bio-card");
    // A tooltip cannot be opened by touch, so it must not be the only carrier.
    expect(card.getAttribute("title")).toBeNull();
  });

  it("uses the marker's own disease when the extractor supplies one", () => {
    const { container } = render(
      <PatientProfile profile={profile({ biomarkers: [{ ...FULL_MARKER, disease: "breast (primary)" }] })} />
    );
    expect(within(container.querySelector(".bio-card")).getByText("breast (primary)")).toBeInTheDocument();
  });

  it("labels an inherited disease context as such", () => {
    const { container } = render(<PatientProfile profile={profile({ biomarkers: [FULL_MARKER] })} />);
    const card = container.querySelector(".bio-card");
    expect(within(card).getByText(/breast cancer/)).toBeInTheDocument();
    expect(within(card).getByText(/from chart diagnosis/)).toBeInTheDocument();
  });

  it("says 'not recorded' rather than silently omitting a missing date", () => {
    const { container } = render(
      <PatientProfile profile={profile({ biomarkers: [{ name: "BRCA", status: "negative" }] })} />
    );
    const card = container.querySelector(".bio-card");
    // An undated, unsourced marker must look different from a fully sourced one.
    expect(within(card).getAllByText("not recorded").length).toBeGreaterThanOrEqual(3);
  });

  it("keeps direction visually distinct", () => {
    const { container } = render(
      <PatientProfile profile={profile({
        biomarkers: [
          { name: "HER2", status: "positive" },
          { name: "ER", status: "negative" },
          { name: "HER2-low", status: "low" },
        ],
      })} />
    );
    const cards = Array.from(container.querySelectorAll(".bio-card"));
    expect(cards[0].className).toContain("bio-pos");
    expect(cards[1].className).toContain("bio-neg");
    expect(cards[2].className).toContain("bio-low");
  });

  it("flags non-current timing on the card", () => {
    const { container } = render(
      <PatientProfile profile={profile({ biomarkers: [{ ...FULL_MARKER, timing: "historical" }] })} />
    );
    const card = container.querySelector(".bio-card");
    expect(card.className).toContain("past");
    expect(within(card).getByText("historical")).toBeInTheDocument();
  });
});

describe("fact evidence", () => {
  it("shows the evidence snippet instead of hiding it in a tooltip", () => {
    render(
      <PatientProfile profile={profile({
        facts: [{ fact_type: "biomarker.HER2", value: "positive", review_state: "confirmed", evidence: "HER2 IHC 3+" }],
      })} />
    );
    expect(screen.getByText(/HER2 IHC 3\+/)).toBeInTheDocument();
  });

  it("marks facts with no snippet honestly", () => {
    render(
      <PatientProfile profile={profile({
        facts: [{ fact_type: "ecog", value: "1", review_state: "inferred" }],
      })} />
    );
    expect(screen.getByText(/no source snippet/i)).toBeInTheDocument();
  });
});

describe("therapy toxicity", () => {
  it("shows toxicity inline rather than on hover only", () => {
    render(
      <PatientProfile profile={profile({ therapies: [{ name: "doxorubicin", caused_toxicity: "cardiomyopathy" }] })} />
    );
    expect(screen.getByText(/toxicity: cardiomyopathy/i)).toBeInTheDocument();
  });
});
