import React, { useState } from "react";

const FIT_CLASS = {
  "Strong fit": "fit-strong",
  Promising: "fit-mid",
  Conditional: "fit-cond",
  "Low fit": "fit-low",
  "Conflicting requirement": "fit-conflict",
};

export default function TrialBoard({ match, shortlist, onToggle }) {
  const { results, candidate_count, trial_count, degraded_mode, fallback_hint,
          needs_review, review_reasons } = match;

  if (needs_review) {
    return (
      <section id="board" className="panel">
        <div className="panel-head">
          <h2>4 · Needs Review — Insufficient Data</h2>
          <p>Not enough verified facts to rank trials confidently. Abstention is intentional — the system does not guess when core data is missing.</p>
        </div>
        <div className="needs-review">
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
        <p>
          {results.length} shown · {candidate_count} cleared disease + purpose gates · {trial_count.toLocaleString()} indexed
          {degraded_mode ? " · deterministic explanations (no LLM key)" : " · LLM-reranked"}
        </p>
      </div>

      {results.length === 0 && (
        <div className="empty">{fallback_hint || "No trials matched. Try broadening the chart or filters."}</div>
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
  return (
    <article className={`card ${r.contraindications?.length ? "card-conflict" : ""}`}>
      <div className="card-top">
        <div className="rank-row">
          <span className="rank">#{r.rank}</span>
          <span className="conf" title="Match score — reflects fit, NOT eligibility probability">
            {r.match_score.toFixed(0)} <em>match</em>
          </span>
          <span className={`fit ${FIT_CLASS[r.fit_label] || ""}`}>{r.fit_label}</span>
        </div>
        <button className={`shortlist ${inShortlist ? "on" : ""}`} onClick={onToggle}>
          {inShortlist ? "✓ Shortlisted" : "+ Shortlist"}
        </button>
      </div>

      <h3 className="card-title">
        <a href={r.url || "#"} target="_blank" rel="noreferrer">{r.title}</a>
      </h3>
      <div className="pills">
        <span className="pill mono">{r.nct}</span>
        {r.disease_family && <span className="pill disease">{r.disease_family}</span>}
        {r.study_purpose && r.study_purpose !== "unknown" && (
          <span className={`pill purpose ${r.study_purpose === "treatment" ? "treat" : ""}`}>
            {r.study_purpose.replace(/_/g, " ")}
          </span>
        )}
        <span className="pill status">{r.status}</span>
        <span className="pill">{r.phase}</span>
      </div>

      {r.contraindications?.length > 0 && (
        <div className="conflict-box">
          ⚠ {r.contraindications.join(" ")}
        </div>
      )}

      <p className="summary">{r.brief_summary}</p>

      {r.reasons?.length > 0 && (
        <div className="block">
          <h4>Why it fits</h4>
          <ul className="reasons">{r.reasons.map((x, i) => <li key={i}>{x}</li>)}</ul>
        </div>
      )}

      {r.cautions?.length > 0 && (
        <div className="block">
          <h4>Cautions to verify</h4>
          <ul className="cautions">{r.cautions.map((x, i) => <li key={i}>{x}</li>)}</ul>
        </div>
      )}

      <button className="link toggle" onClick={() => setOpen((o) => !o)}>
        {open ? "Hide details" : "Score breakdown & details"}
      </button>

      {open && (
        <div className="details">
          <div className="breakdown">
            {Object.entries(r.breakdown).map(([k, v]) => (
              <div className="bar-row" key={k}>
                <span>{k.replace(/_/g, " ")}</span>
                <div className="bar"><div className="fill" style={{ width: `${Math.max(0, Math.min(100, Math.abs(v)))}%` }} /></div>
                <span className="bar-val">{v}</span>
              </div>
            ))}
          </div>
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

function Meta({ label, value }) {
  return (
    <div className="meta">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
