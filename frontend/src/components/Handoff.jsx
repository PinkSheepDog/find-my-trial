import React, { useMemo, useState } from "react";

// Physician handoff: the shortlisted trials, patient summary, top reasons and the
// manual checks per trial, with a copy/export action and a non-dismissible
// final-eligibility warning.
export default function Handoff({ profile, results, shortlist }) {
  const [copied, setCopied] = useState(false);
  const selected = useMemo(
    () => results.filter((r) => shortlist.includes(r.nct)),
    [results, shortlist]
  );

  const summary = useMemo(() => buildSummary(profile, selected), [profile, selected]);

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
            <button className="primary" onClick={copy}>{copied ? "Copied ✓" : "Copy summary"}</button>
          </div>
          <textarea className="handoff-text" rows={Math.min(20, 6 + selected.length * 4)} readOnly value={summary} />
        </>
      )}
    </section>
  );
}

function buildSummary(profile, selected) {
  const lines = [];
  lines.push("FIND MY TRIAL — SHORTLIST HANDOFF (decision support, not eligibility)");
  lines.push("");
  lines.push("PATIENT (de-identified):");
  lines.push("  " + (profile.summary_line || compactProfile(profile)));
  const pos = (profile.biomarkers || []).filter((b) => b.status === "positive").map((b) => b.name);
  const neglow = (profile.biomarkers || []).filter((b) => ["negative", "low"].includes(b.status)).map((b) => `${b.name} ${b.status}`);
  if (pos.length) lines.push("  Positive markers: " + pos.join(", "));
  if (neglow.length) lines.push("  Negative/low markers: " + neglow.join(", "));
  lines.push("");
  selected.forEach((r, i) => {
    lines.push(`${i + 1}. ${r.nct} — ${r.title}`);
    lines.push(`   Match score: ${r.match_score.toFixed(0)}/100 (${r.fit_label}, NOT eligibility) · ${r.status} · ${r.phase}`);
    if (r.reasons?.length) lines.push("   Reasons: " + r.reasons.join("; "));
    if (r.cautions?.length) lines.push("   Manual checks: " + r.cautions.join("; "));
    if (r.contraindications?.length) lines.push("   ⚠ Conflict: " + r.contraindications.join(" "));
    if (r.url) lines.push("   " + r.url);
    lines.push("");
  });
  return lines.join("\n");
}

function compactProfile(p) {
  return [
    [p.age, p.sex].filter(Boolean).join(" "),
    p.diagnosis,
    p.ecog != null ? `ECOG ${p.ecog}` : null,
  ].filter(Boolean).join("; ");
}
