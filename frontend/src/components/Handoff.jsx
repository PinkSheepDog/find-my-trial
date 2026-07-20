import React, { useMemo, useState } from "react";
import { notesToTextList } from "../lib/notes.js";
import { corpusFreshness, corpusProvenance } from "../lib/freshness.js";

const APP_VERSION = typeof __APP_VERSION__ === "string" ? __APP_VERSION__ : "dev";

// Physician handoff: the shortlisted trials, patient summary, top reasons and the
// manual checks per trial, with a copy/export action and a non-dismissible
// final-eligibility warning.
export default function Handoff({ profile, results, shortlist, health, filters }) {
  const [copied, setCopied] = useState(false);
  const selected = useMemo(
    () => results.filter((r) => shortlist.includes(r.nct)),
    [results, shortlist]
  );

  const summary = useMemo(
    () => buildSummary(profile, selected, { health, filters }),
    [profile, selected, health, filters]
  );

  async function copy() {
    try {
      await navigator.clipboard.writeText(summary);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard may be unavailable; the textarea below is always selectable */
    }
  }

  return (
    <section id="handoff" className="panel handoff">
      <div className="panel-head">
        <h2>5 · Care-Team Handoff</h2>
        <p>{selected.length ? `${selected.length} trial(s) shortlisted.` : "Shortlist trials on the board to build a handoff."}</p>
      </div>

      <div className="final-warning">
        ⚠ Decision support only — this is not an eligibility determination. Confirm disease setting,
        line of therapy, biomarker requirements, prior-exposure exclusions, organ-function thresholds,
        and site feasibility against the full protocol before outreach.
      </div>

      {selected.length > 0 && (
        <>
          <div className="handoff-actions">
            <button type="button" className="primary" onClick={copy}>{copied ? "Copied ✓" : "Copy summary"}</button>
            <span className="sr-only" role="status" aria-live="polite">
              {copied ? "Handoff summary copied to clipboard." : ""}
            </span>
          </div>
          <label className="sr-only" htmlFor="handoff-summary">Handoff summary text</label>
          <textarea
            id="handoff-summary"
            className="handoff-text"
            rows={Math.min(24, 10 + selected.length * 4)}
            readOnly
            value={summary}
          />
        </>
      )}
    </section>
  );
}

/**
 * An export outlives the session it came from. Without provenance a reader
 * cannot tell whether the corpus was a week or a year old, whether an LLM or the
 * deterministic rules wrote the explanations, or which filters shaped the list —
 * so the same text could be re-read months later as if it were current. The
 * PROVENANCE block records all of it.
 */
export function buildSummary(profile, selected, { health, filters, now = new Date() } = {}) {
  const fresh = corpusFreshness(health, now);
  const prov = corpusProvenance(health);
  const lines = [];
  lines.push("FIND MY TRIAL — SHORTLIST HANDOFF (decision support, not eligibility)");
  lines.push("");

  lines.push("PROVENANCE:");
  lines.push(`  Generated: ${now.toISOString()}`);
  lines.push(`  Trial corpus current through: ${fresh.known ? fresh.label : "UNKNOWN — verify on ClinicalTrials.gov"}`);
  if (fresh.ageDays != null) lines.push(`  Corpus age at export: ${fresh.ageDays} day(s)`);
  if (health && health.trial_count != null) lines.push(`  Trials indexed: ${Number(health.trial_count).toLocaleString()}`);
  if (fresh.normalizationVersion) lines.push(`  Index/normalization version: ${fresh.normalizationVersion}`);
  if (prov.indexBuiltAt) lines.push(`  Index built at: ${prov.indexBuiltAt}`);
  if (prov.contentHash) lines.push(`  Corpus content hash: ${prov.contentHash}`);
  lines.push(`  Corpus integrity: ${prov.integrityLabel}`);
  lines.push(`  Profile extractor: ${profile?.extractor || "unknown"}`);
  lines.push(`  Explanation source: ${explanationSource(selected, health)}`);
  lines.push(`  Filters applied: ${describeFilters(filters)}`);
  lines.push(`  Server version: ${prov.appVersion || "unknown"}`);
  lines.push(`  Workspace build: ${APP_VERSION}`);
  if (prov.deidReviewEnforced === false) {
    lines.push("  ⚠ Server-side de-identification review gate was DISABLED for this run.");
  }
  if (fresh.stale) lines.push("  ⚠ Corpus may be stale — re-verify recruitment status before outreach.");
  lines.push("");

  lines.push("PATIENT (de-identified):");
  lines.push("  " + (profile.summary_line || compactProfile(profile)));
  const pos = (profile.biomarkers || []).filter((b) => b.status === "positive").map(describeMarker);
  const neglow = (profile.biomarkers || []).filter((b) => ["negative", "low"].includes(b.status)).map(describeMarker);
  if (pos.length) lines.push("  Positive markers: " + pos.join("; "));
  if (neglow.length) lines.push("  Negative/low markers: " + neglow.join("; "));
  lines.push("");

  selected.forEach((r, i) => {
    lines.push(`${i + 1}. ${r.nct} — ${r.title}`);
    lines.push(`   Match score: ${Number(r.match_score ?? 0).toFixed(0)}/100 (${r.fit_label}, NOT eligibility) · ${r.status} · ${r.phase}`);
    const reasons = notesToTextList(r.reasons);
    const cautions = notesToTextList(r.cautions);
    const conflicts = notesToTextList(r.contraindications);
    if (reasons.length) lines.push("   Reasons: " + reasons.join("; "));
    if (cautions.length) lines.push("   Manual checks: " + cautions.join("; "));
    if (conflicts.length) lines.push("   ⚠ Conflict: " + conflicts.join(" "));
    if (r.url) lines.push("   " + r.url);
    lines.push("");
  });
  return lines.join("\n");
}

// Biomarker provenance travels with the export: a marker without a date or
// specimen must not read as freshly confirmed once it is pasted elsewhere.
function describeMarker(b) {
  const bits = [`${b.name} ${b.status}`];
  if (b.detail) bits.push(b.detail);
  if (b.method) bits.push(b.method);
  if (b.specimen) bits.push(b.specimen);
  bits.push(b.date ? `dated ${b.date}` : "date not recorded");
  if (b.timing && b.timing !== "current") bits.push(String(b.timing));
  return bits.join(", ");
}

function explanationSource(selected, health) {
  const kinds = new Set(selected.map((r) => r.explained_by).filter(Boolean));
  const label = kinds.size ? Array.from(kinds).sort().join(" + ") : "unknown";
  const degraded = health?.degraded_mode ? " (degraded mode: no LLM key)" : "";
  return label + degraded;
}

function describeFilters(filters) {
  if (!filters) return "unknown";
  return Object.entries(filters)
    .map(([k, v]) => `${k}=${v === "" ? "(any)" : v}`)
    .join(", ");
}

function compactProfile(p) {
  return [
    [p.age, p.sex].filter(Boolean).join(" "),
    p.diagnosis,
    p.ecog != null ? `ECOG ${p.ecog}` : null,
  ].filter(Boolean).join("; ");
}
