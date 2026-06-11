import React from "react";

// The HUMAN GATE. This is exactly the text that will leave the machine (to the LLM).
// The user can see redactions, edit anything the rules missed, then explicitly approve.
export default function DeidReview({ deid, onApprove, onEdit, busy, hasResult }) {
  return (
    <section id="review" className="panel review-panel">
      <div className="panel-head">
        <h2>2 · De-identification Review</h2>
        <p>
          This is the only text that leaves your machine. Identifiers are replaced with tags.
          Review and edit before approving — automated de-identification is a safety net, not a guarantee.
        </p>
      </div>

      <div className="deid-summary">
        <strong>{deid.total_redactions}</strong> identifier{deid.total_redactions === 1 ? "" : "s"} removed
        {deid.redaction_summary && <span className="muted"> — {deid.redaction_summary}</span>}
      </div>

      <textarea
        className="deid-text"
        rows={10}
        value={deid.deidentified_text}
        onChange={(e) => onEdit(e.target.value)}
      />

      <div className="review-actions">
        <span className="muted small">
          By approving, you confirm this text contains no patient identifiers.
        </span>
        <button className="primary" disabled={busy} onClick={onApprove}>
          {hasResult ? "Re-run match" : "Approve & match trials →"}
        </button>
      </div>
    </section>
  );
}
