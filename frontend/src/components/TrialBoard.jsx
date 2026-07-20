import React, { useState } from "react";
import { normalizeNotes } from "../lib/notes.js";

const FIT_CLASS = {
  "Strong fit": "fit-strong",
  Promising: "fit-mid",
  Conditional: "fit-cond",
  "Low fit": "fit-low",
  "Conflicting requirement": "fit-conflict",
};

// Study-purpose taxonomy. Every value gets a label — including `unknown`, which
// is shown as "Purpose unknown" rather than suppressed: an unclassified study is
// a fact the reviewer needs, not an absence to hide.
const PURPOSE_LABELS = {
  treatment: "Treatment",
  expanded_access: "Expanded access",
  diagnostic: "Diagnostic",
  imaging: "Imaging",
  screening: "Screening",
  prevention: "Prevention",
  supportive_care: "Supportive care",
  basic_science: "Basic science",
  health_services_research: "Health services research",
  device_feasibility: "Device feasibility",
  observational: "Observational",
  registry: "Registry",
  other: "Other purpose",
  unknown: "Purpose unknown",
};

const PURPOSE_CLASS = {
  treatment: "treat",
  expanded_access: "treat",
  imaging: "imaging",
  diagnostic: "diagnostic",
  registry: "registry",
  observational: "registry",
  unknown: "unknown",
};

const IMAGING_RE = /\b(imaging|pet[/ -]?ct|pet\b|mri\b|spect\b|scintigraph|radiotracer|tracer|contrast agent|ultrasound|radiolog)/i;
const REGISTRY_RE = /\b(registry|registries)\b/i;

/**
 * Resolve the purpose to display. An explicit backend value always wins; the
 * imaging/registry refinements only fire when the registry data itself says so
 * and the coarser value ("diagnostic" / "observational") would have hidden it.
 */
export function resolvePurpose(r) {
  const raw = (r?.study_purpose || "unknown").toLowerCase();
  if (raw in PURPOSE_LABELS && raw !== "diagnostic" && raw !== "observational") return raw;

  const haystack = [r?.title, r?.brief_summary, r?.study_type, ...(r?.interventions || [])]
    .filter(Boolean).join(" ");

  if (raw === "diagnostic") return IMAGING_RE.test(haystack) ? "imaging" : "diagnostic";
  if (raw === "observational") return REGISTRY_RE.test(haystack) ? "registry" : "observational";
  return raw in PURPOSE_LABELS ? raw : "unknown";
}

export default function TrialBoard({ match, shortlist, onToggle }) {
  const { results, candidate_count, trial_count, degraded_mode, fallback_hint,
          needs_review, review_reasons, location_query, location_match_count,
          location_notice } = match;

  if (needs_review) {
    return (
      <section id="board" className="panel">
        <div className="panel-head">
          <h2>4 · Needs Review — Insufficient Data</h2>
          <p>Not enough verified facts to rank trials confidently. Abstention is intentional — the system does not guess when core data is missing.</p>
        </div>
        <div className="needs-review" role="status">
          <strong>Resolve and re-run:</strong>
          <ul>{(review_reasons || []).map((x, i) => <li key={i}>{x}</li>)}</ul>
        </div>
      </section>
    );
  }

  return (
    <section id="board" className="panel">
      <div className="panel-head">
        <h2>4 · Ranked Trial Board</h2>
        <p role="status">
          {results.length} shown · {candidate_count} cleared disease + purpose gates · {Number(trial_count || 0).toLocaleString()} indexed
          {degraded_mode ? " · deterministic explanations (no LLM key)" : " · LLM-reranked"}
          {location_query ? ` · ${location_match_count || 0} with a site matching “${location_query}”` : ""}
        </p>
      </div>

      {/* Server-authored explanation of what the location filter did. Saying "no
          site in Alaska among these results" is more use than an empty board. */}
      {location_notice && <div className="location-notice" role="status">{location_notice}</div>}

      {results.length === 0 && (
        <div className="empty" role="status">{fallback_hint || "No trials matched. Try broadening the chart or filters."}</div>
      )}

      <div className="cards">
        {results.map((r) => (
          <TrialCard key={r.nct} r={r} inShortlist={shortlist.includes(r.nct)} onToggle={() => onToggle(r.nct)} />
        ))}
      </div>
    </section>
  );
}

function TrialCard({ r, inShortlist, onToggle }) {
  const [open, setOpen] = useState(false);
  // reasons/cautions/contraindications may arrive as plain strings or as objects
  // carrying a verbatim evidence snippet — normalizeNotes accepts both.
  const reasons = normalizeNotes(r.reasons);
  const cautions = normalizeNotes(r.cautions);
  const contraindications = normalizeNotes(r.contraindications);
  // Flattened verbatim quotes. Ungrounded entries are excluded — an unverified
  // quote must never appear in a section headed "Source quotes".
  const evidence = normalizeNotes(r.evidence).filter((e) => e.grounded !== false && e.evidence);
  const purpose = resolvePurpose(r);
  const detailsId = `details-${r.nct}`;

  return (
    <article className={`card ${contraindications.length ? "card-conflict" : ""}`}>
      <div className="card-top">
        <div className="rank-row">
          <span className="rank">#{r.rank}</span>
          <span className="conf" title="Match score — reflects fit, NOT eligibility probability">
            {Number(r.match_score ?? 0).toFixed(0)} <em>match</em>
          </span>
          <span className={`fit ${FIT_CLASS[r.fit_label] || ""}`}>{r.fit_label}</span>
        </div>
        <button
          type="button"
          className={`shortlist ${inShortlist ? "on" : ""}`}
          aria-pressed={inShortlist}
          onClick={onToggle}
        >
          {inShortlist ? "✓ Shortlisted" : "+ Shortlist"}
        </button>
      </div>

      <h3 className="card-title">
        <a href={r.url || "#"} target="_blank" rel="noreferrer">{r.title}</a>
      </h3>
      <div className="pills">
        <span className="pill mono">{r.nct}</span>
        {r.disease_family && <span className="pill disease">{r.disease_family}</span>}
        {/* The trial's disease could not be classified — the disease gate did not
            actually vouch for this one, so say so rather than showing nothing. */}
        {r.disease_unclassified && (
          <span className="pill caveat" title="The trial's disease area could not be classified from the registry record">
            disease unclassified
          </span>
        )}
        <span className={`pill purpose ${PURPOSE_CLASS[purpose] || ""}`}>
          {PURPOSE_LABELS[purpose] || purpose.replace(/_/g, " ")}
          {/* Inferred, not stated by the registry. */}
          {r.purpose_unverified ? <span className="pill-caveat"> (inferred)</span> : null}
        </span>
        <span className="pill status">{r.status}</span>
        {r.location_match && r.matched_locations?.length > 0 && (
          <span className="pill location-match" title={r.matched_locations.join("; ")}>
            site near you: {r.matched_locations[0]}
            {r.matched_locations.length > 1 ? ` +${r.matched_locations.length - 1}` : ""}
          </span>
        )}
        <span className="pill">{r.phase}</span>
      </div>

      {contraindications.length > 0 && (
        <div className="conflict-box">
          <span aria-hidden="true">⚠ </span>
          <span className="sr-only">Conflict: </span>
          <NoteList notes={contraindications} inline />
        </div>
      )}

      <p className="summary">{r.brief_summary}</p>

      {reasons.length > 0 && (
        <div className="block">
          <h4>Why it fits</h4>
          <ul className="reasons"><NoteItems notes={reasons} /></ul>
        </div>
      )}

      {cautions.length > 0 && (
        <div className="block">
          <h4>Cautions to verify</h4>
          <ul className="cautions"><NoteItems notes={cautions} /></ul>
        </div>
      )}

      {/* Withheld claims are reported, not silently discarded: "3 statements were
          dropped" tells the reviewer the explanation is partial. */}
      {r.ungrounded_dropped > 0 && (
        <p className="small muted dropped-note">
          {r.ungrounded_dropped} generated statement{r.ungrounded_dropped === 1 ? " was" : "s were"} withheld —
          no verbatim trial text supported {r.ungrounded_dropped === 1 ? "it" : "them"}.
        </p>
      )}

      <button
        type="button"
        className="link toggle"
        aria-expanded={open}
        aria-controls={detailsId}
        onClick={() => setOpen((o) => !o)}
      >
        {open ? "Hide details" : "Score breakdown & details"}
      </button>

      {open && (
        <div className="details" id={detailsId}>
          <div className="breakdown">
            {Object.entries(r.breakdown || {}).map(([k, v]) => (
              <BreakdownRow key={k} label={k} value={Number(v) || 0} />
            ))}
          </div>
          <p className="breakdown-legend small muted">
            Positive contributions add to the match score; <span className="legend-swatch neg" aria-hidden="true" />
            penalties subtract from it.
          </p>
          {evidence.length > 0 && (
            <div className="block evidence-block">
              <h4>Source quotes</h4>
              <ul className="evidence-list">
                {evidence.map((e, i) => (
                  <li key={i}>
                    <span className="note-evidence-label">{e.source || "trial record"}</span>
                    <q>{e.evidence || e.text}</q>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="meta-grid">
            <Meta label="Sponsor" value={r.sponsor || "—"} />
            <Meta label="Eligibility sex" value={r.eligibility_sex} />
            <Meta label="Eligibility age" value={r.eligibility_age} />
            <Meta label="Locations" value={r.locations?.join("; ") || "—"} />
            <Meta label="Conditions" value={r.conditions?.join(", ") || "—"} />
            <Meta label="Interventions" value={r.interventions?.join(", ") || "—"} />
          </div>
        </div>
      )}
    </article>
  );
}

// A negative contribution (e.g. contraindication_penalty) must not draw as a
// positive-width bar: it is coloured as a penalty, grows leftward from the
// centre line, and carries an explicit minus sign.
function BreakdownRow({ label, value }) {
  const negative = value < 0;
  const width = Math.max(0, Math.min(100, Math.abs(value)));
  const pretty = label.replace(/_/g, " ");
  const signed = `${value > 0 ? "+" : ""}${value}`;
  return (
    <div className={`bar-row ${negative ? "is-penalty" : ""}`}>
      <span>{pretty}</span>
      <div className={`bar ${negative ? "bar-neg" : ""}`}>
        <div
          className={`fill ${negative ? "fill-neg" : ""}`}
          style={{ width: `${width}%` }}
        />
      </div>
      <span className="bar-val">
        <span className="sr-only">{negative ? "penalty " : "contribution "}</span>
        {signed}
      </span>
    </div>
  );
}

function NoteItems({ notes }) {
  return notes.map((n, i) => (
    <li key={i}>
      <span className="note-text">{n.text}</span>
      {/* An UNGROUNDED claim never shows its snippet: the server could not verify
          that quote against the trial record, and a quote shown as evidence reads
          as verified. It is labelled as unverified instead. */}
      {n.grounded === false ? (
        <span className="note-evidence ungrounded">
          <span className="note-evidence-label">unverified</span>
          not backed by verbatim trial text
        </span>
      ) : n.evidence ? (
        <span className="note-evidence">
          <span className="note-evidence-label">
            {n.source ? `evidence · ${n.source}` : "evidence"}
          </span>
          <q>{n.evidence}</q>
        </span>
      ) : null}
    </li>
  ));
}

function NoteList({ notes, inline }) {
  return (
    <span className={inline ? "note-inline" : ""}>
      {notes.map((n, i) => (
        <span key={i} className="note-inline-item">
          {n.text}
          {n.evidence && <q className="note-evidence-inline">{n.evidence}</q>}
          {i < notes.length - 1 ? " " : ""}
        </span>
      ))}
    </span>
  );
}

function Meta({ label, value }) {
  return (
    <div className="meta">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
