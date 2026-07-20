import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../src/api.js", () => ({
  api: {
    health: vi.fn(),
    me: vi.fn(),
    capabilities: vi.fn(),
    deidentify: vi.fn(),
    approveDeid: vi.fn(),
    match: vi.fn(),
    logout: vi.fn(),
    extractText: vi.fn(),
  },
}));

import App from "../src/App.jsx";
import { api } from "../src/api.js";

const HEALTH = {
  ok: true,
  trial_count: 4321,
  llm_enabled: false,
  degraded_mode: true,
  data_current_through: "2026-07-10",
  normalization_version: "1.1.0-disease-purpose-gates",
  app_version: "2.3.0",
  corpus_content_hash: "abc123",
  index_built_at: "2026-07-11T00:00:00Z",
  corpus_integrity_verified: true,
  deid_review_enforced: true,
};

const MATCH_RESULT = {
  profile: { extractor: "rules", summary_line: "62 F, breast", biomarkers: [], facts: [] },
  match: {
    results: [],
    candidate_count: 0,
    trial_count: 4321,
    semantic_used: false,
    degraded_mode: true,
    needs_review: false,
    review_reasons: [],
  },
};

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function httpError(message, status, errorId) {
  const e = new Error(message);
  e.status = status;
  e.errorId = errorId;
  return e;
}

beforeEach(() => {
  api.health.mockResolvedValue(HEALTH);
  api.me.mockResolvedValue({ username: "dr.smith" });
  api.capabilities.mockResolvedValue({
    components: {
      schemas: {
        MatchRequest: {
          properties: {
            deidentified_text: {}, top_k: {}, active_only: {}, recruiting_only: {},
            interventional_only: {}, treatment_only: {}, location: {}, location_required: {},
          },
        },
      },
    },
  });
  api.deidentify.mockResolvedValue({
    deidentified_text: "62yo F with [REDACTED] breast cancer",
    redaction_summary: "1 name",
    redaction_counts: { name: 1 },
    total_redactions: 1,
  });
  api.approveDeid.mockResolvedValue({ approval_token: "tok-123", expires_in_minutes: 30, residual_redactions: 0 });
  api.match.mockResolvedValue(MATCH_RESULT);
});

async function renderApp() {
  render(<App />);
  await screen.findByRole("heading", { name: /Clinical Trial Review Workspace/i });
}

async function reachReview() {
  await renderApp();
  fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
  fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));
  return screen.findByRole("heading", { name: /De-identification Review/i });
}

describe("screen-reader announcements", () => {
  it("announces work in progress via a polite status region", async () => {
    const d = deferred();
    api.deidentify.mockReturnValue(d.promise);
    await renderApp();

    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));

    const busy = await screen.findByText(/Removing identifiers for review/i);
    const region = busy.closest('[role="status"]');
    expect(region).toBeTruthy();
    expect(region).toHaveAttribute("aria-live", "polite");

    d.resolve({ deidentified_text: "x", redaction_summary: "", redaction_counts: {}, total_redactions: 0 });
    await screen.findByRole("heading", { name: /De-identification Review/i });
  });

  it("announces errors with role=alert", async () => {
    api.deidentify.mockRejectedValue(httpError("Service unavailable", 503, "SRV-9001"));
    await renderApp();
    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Service unavailable");
  });

  it("announces the result count in a live region", async () => {
    const stub = (nct) => ({
      rank: 1, nct, title: `Trial ${nct}`, url: "", status: "RECRUITING", phase: "Phase 2",
      study_type: "INTERVENTIONAL", sponsor: "S", brief_summary: "", conditions: [],
      interventions: [], locations: [], match_score: 70, fit_label: "Promising",
      reasons: [], cautions: [], contraindications: [], breakdown: {},
      eligibility_sex: "ALL", eligibility_age: "18+", disease_family: "breast",
      study_purpose: "treatment", explained_by: "rules",
    });
    api.match.mockResolvedValue({
      ...MATCH_RESULT,
      match: { ...MATCH_RESULT.match, results: [stub("NCT1"), stub("NCT2")] },
    });
    await reachReview();
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    const announcer = screen.getByTestId("result-announcer");
    await waitFor(() => expect(announcer).toHaveTextContent("2 trials ranked"));
    expect(announcer).toHaveAttribute("aria-live", "polite");
    expect(announcer).toHaveAttribute("role", "status");
  });
});

describe("error identity", () => {
  it("shows the server-issued error_id so a user can report without pasting chart text", async () => {
    api.deidentify.mockRejectedValue(httpError("Upstream failure", 502, "SRV-4242"));
    await reachReviewFailure();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("SRV-4242");
    // The chart text must never appear in an error.
    expect(alert.textContent).not.toContain("secret-chart-text");
  });

  it("falls back to a client ID when the failure never reached the server", async () => {
    api.deidentify.mockRejectedValue(new Error("Could not reach the server. Check your connection and retry."));
    await reachReviewFailure();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/FMT-[0-9A-F]{6}/);
  });

  async function reachReviewFailure() {
    await renderApp();
    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "secret-chart-text" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));
  }
});

describe("cancel and retry", () => {
  it("offers a cancel affordance while a request is in flight", async () => {
    api.deidentify.mockReturnValue(deferred().promise);
    await renderApp();
    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));

    expect(await screen.findByRole("button", { name: /cancel request/i })).toBeInTheDocument();
  });

  it("treats a cancelled request as a non-error", async () => {
    const abort = new Error("Request cancelled.");
    abort.name = "AbortError";
    api.deidentify.mockRejectedValue(abort);

    await renderApp();
    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));

    await waitFor(() => expect(api.deidentify).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers an explicit retry that re-issues the failed request", async () => {
    api.deidentify.mockRejectedValueOnce(httpError("Temporary failure", 503, "SRV-1"));
    await renderApp();
    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));

    const retry = await screen.findByRole("button", { name: /^retry$/i });
    fireEvent.click(retry);

    await screen.findByRole("heading", { name: /De-identification Review/i });
    expect(api.deidentify).toHaveBeenCalledTimes(2);
  });
});

// The server binds an approval token to a digest of the reviewed text and
// refuses /api/match without it. The UI must make that an affirmative act.
describe("de-identification approval gate", () => {
  it("approves the exact reviewed text and sends the token with the match", async () => {
    await reachReview();
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await waitFor(() => expect(api.match).toHaveBeenCalled());
    expect(api.approveDeid).toHaveBeenCalledWith(
      "62yo F with [REDACTED] breast cancer",
      expect.anything()
    );
    const [, options] = api.match.mock.calls[0];
    expect(options.approvalToken).toBe("tok-123");
  });

  it("re-approves the edited text rather than reusing a stale token", async () => {
    await reachReview();
    fireEvent.change(screen.getByLabelText(/de-identified text/i), { target: { value: "edited text" } });
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await waitFor(() => expect(api.approveDeid).toHaveBeenCalledWith("edited text", expect.anything()));
  });

  it("stops at a 422 refusal and never sends the text to match", async () => {
    api.approveDeid.mockRejectedValue(
      httpError("Cannot approve: 2 identifier(s) still present (2 names). Re-run de-identification before approving.", 422, "SRV-422")
    );
    await reachReview();
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await screen.findByText(/Approval refused by the server/i);
    // The refusal detail (counts by category, no chart text) is shown verbatim.
    expect(screen.getAllByText(/2 identifier\(s\) still present/i).length).toBeGreaterThan(0);
    expect(api.match).not.toHaveBeenCalled();
  });

  it("sends the user back to re-review on a 403 rather than retrying silently", async () => {
    api.match.mockRejectedValue(httpError("Approval expired", 403, "SRV-403"));
    await reachReview();
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await screen.findByText(/Approval no longer valid/i);
    const alerts = await screen.findAllByRole("alert");
    expect(
      alerts.some((a) => /Review the de-identified text again and re-approve/i.test(a.textContent))
    ).toBe(true);
    expect(api.match).toHaveBeenCalledTimes(1);
  });

  it("confirms approval visibly once granted", async () => {
    await reachReview();
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));
    expect(await screen.findByText(/Approved for matching/i)).toBeInTheDocument();
  });

  it("withdraws the approval badge when the text is edited afterwards", async () => {
    await reachReview();
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));
    await screen.findByText(/Approved for matching/i);

    fireEvent.change(screen.getByLabelText(/de-identified text/i), { target: { value: "changed" } });
    expect(screen.queryByText(/Approved for matching/i)).toBeNull();
  });

  it("skips the approval call when the server reports the gate disabled", async () => {
    api.health.mockResolvedValue({ ...HEALTH, deid_review_enforced: false });
    await reachReview();
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await waitFor(() => expect(api.match).toHaveBeenCalled());
    expect(api.approveDeid).not.toHaveBeenCalled();
    expect(screen.getByText(/approval gate is off/i)).toBeInTheDocument();
  });

  it("proceeds when an older server has no approval endpoint", async () => {
    api.approveDeid.mockRejectedValue(httpError("Not found", 404, "SRV-404"));
    await reachReview();
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await waitFor(() => expect(api.match).toHaveBeenCalled());
    expect(api.match.mock.calls[0][1].approvalToken).toBeUndefined();
  });
});

describe("filters", () => {
  it("sends every filter explicitly, matching the server defaults", async () => {
    await reachReview();
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await waitFor(() => expect(api.match).toHaveBeenCalled());
    const [payload] = api.match.mock.calls[0];
    expect(payload).toMatchObject({
      top_k: 10,
      active_only: true,
      interventional_only: true,
      treatment_only: true,
      location: "",
    });
  });

  it("exposes the treatment-only control the server was silently defaulting", async () => {
    await renderApp();
    const control = screen.getByLabelText(/study type/i);
    expect(control).toHaveValue("treatment");

    // Widening past treatment-only must actually reach the server as false.
    fireEvent.change(control, { target: { value: "interventional" } });
    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));
    await screen.findByRole("heading", { name: /De-identification Review/i });
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await waitFor(() => expect(api.match).toHaveBeenCalled());
    expect(api.match.mock.calls[0][0].treatment_only).toBe(false);
    expect(api.match.mock.calls[0][0].interventional_only).toBe(true);
  });

  it("widens to every study type when asked", async () => {
    await renderApp();
    fireEvent.change(screen.getByLabelText(/study type/i), { target: { value: "all" } });
    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));
    await screen.findByRole("heading", { name: /De-identification Review/i });
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await waitFor(() => expect(api.match).toHaveBeenCalled());
    expect(api.match.mock.calls[0][0]).toMatchObject({
      treatment_only: false, interventional_only: false,
    });
  });

  it("exposes recruiting_only when the server models it", async () => {
    await renderApp();
    const status = screen.getByLabelText(/recruitment status/i);
    expect(within(status).getByRole("option", { name: /open to enrolment only/i })).toBeInTheDocument();
  });

  // "Open to enrolment" is strictly narrower than "active", so choosing it must set
  // BOTH flags — sending recruiting_only without active_only would be incoherent.
  it("narrowing to open-to-enrolment sends both status flags", async () => {
    await renderApp();
    fireEvent.change(screen.getByLabelText(/recruitment status/i), { target: { value: "recruiting" } });
    expect(screen.getByText(/Admits RECRUITING/i)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));
    await screen.findByRole("heading", { name: /De-identification Review/i });
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await waitFor(() => expect(api.match).toHaveBeenCalled());
    expect(api.match.mock.calls[0][0]).toMatchObject({ recruiting_only: true, active_only: true });
  });

  it("drops the status filter entirely when any status is chosen", async () => {
    await renderApp();
    fireEvent.change(screen.getByLabelText(/recruitment status/i), { target: { value: "any" } });
    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));
    await screen.findByRole("heading", { name: /De-identification Review/i });
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await waitFor(() => expect(api.match).toHaveBeenCalled());
    expect(api.match.mock.calls[0][0]).toMatchObject({ recruiting_only: false, active_only: false });
  });

  it("exposes location_required and explains soft vs hard location filtering", async () => {
    await renderApp();
    const control = await screen.findByLabelText(/location handling/i);
    // Meaningless without a location, so it stays disabled until one is typed.
    expect(control).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/location focus/i), { target: { value: "Detroit, Michigan" } });
    expect(control).toBeEnabled();
    expect(screen.getByText(/can legitimately return no trials/i)).toBeInTheDocument();
  });

  it("sends the location filters the server models", async () => {
    await renderApp();
    fireEvent.change(screen.getByLabelText(/location focus/i), { target: { value: "Detroit" } });
    fireEvent.change(await screen.findByLabelText(/location handling/i), { target: { value: "require" } });
    fireEvent.change(screen.getByLabelText(/chart text/i), { target: { value: "chart" } });
    fireEvent.click(screen.getByRole("button", { name: /de-identify/i }));
    await screen.findByRole("heading", { name: /De-identification Review/i });
    fireEvent.click(screen.getByRole("button", { name: /approve this text/i }));

    await waitFor(() => expect(api.match).toHaveBeenCalled());
    expect(api.match.mock.calls[0][0]).toMatchObject({ location: "Detroit", location_required: true });
  });

  it("hides recruiting_only when the server does not model it", async () => {
    api.capabilities.mockResolvedValue({
      components: { schemas: { MatchRequest: { properties: { deidentified_text: {}, top_k: {}, active_only: {}, interventional_only: {}, treatment_only: {}, location: {} } } } },
    });
    await renderApp();
    await waitFor(() => expect(api.capabilities).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryByRole("option", { name: /open to enrolment only/i })).toBeNull());
  });

  it("names the registry statuses the active filter admits", async () => {
    await renderApp();
    const help = screen.getByText(/Admits RECRUITING/i);
    expect(help).toHaveTextContent("ACTIVE_NOT_RECRUITING");
    expect(help).toHaveTextContent(/closed to new enrolment/i);
  });

  it("degrades to the long-standing filters when the probe fails", async () => {
    api.capabilities.mockRejectedValue(new Error("no openapi"));
    await renderApp();
    expect(
      within(screen.getByLabelText(/study type/i)).getByRole("option", { name: /treatment studies only/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /open to enrolment only/i })).toBeNull();
  });
});

// Corpus freshness / provenance is intentionally not shown on-screen (product
// decision — it remains in the exported handoff summary; see handoff.test.jsx).

describe("skip link", () => {
  it("provides a skip-to-content link as the first focusable element", async () => {
    await renderApp();
    const skip = screen.getByRole("link", { name: /skip to main content/i });
    expect(skip).toHaveAttribute("href", "#main-content");
    expect(document.querySelector("#main-content")).toBeTruthy();
  });
});
