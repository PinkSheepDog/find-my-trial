import React from "react";

// The HUMAN GATE. This is exactly the text that will leave the machine (to the LLM).
// The user can see redactions, edit anything the rules missed, then explicitly approve.
//
// Approval is not a UI formality: pressing the button POSTs this exact text to
// /api/approve-deid, which refuses it outright if identifiers remain and otherwise
// returns a token bound to a digest of the text. Matching without that token is
// rejected by the server. The wording below says so plainly, because a gate the
// user does not recognise as a gate is one they will click through.
export default function DeidReview({ deid, onApprove, onEdit, busy, hasResult, approval, enforced }) {
  const state = approval?.state;

  return (
    <section id="review" className="panel review-panel">
      <div className="panel-head">
        <h2>2 · De-identification Review</h2>
        <p>
          This is the only text that leaves your machine. Identifiers are replaced with tags.
          Review and edit before approving — automated de-identification is a safety net, not a guarantee.
        </p>
      </div>

      <div className="deid-summary" role="status" aria-live="polite">
        <strong>{deid.total_redactions}</strong> identifier{deid.total_redactions === 1 ? "" : "s"} removed
        {deid.redaction_summary && <span className="muted"> — {deid.redaction_summary}</span>}
      </div>

      <label className="sr-only" htmlFor="deid-text">De-identified text for review</label>
      <textarea
        id="deid-text"
        className="deid-text"
        rows={10}
        value={deid.deidentified_text}
        onChange={(e) => onEdit(e.target.value)}
      />

      {state === "refused" && (
        <div className="gate-note refused" role="alert">
          <strong>Approval refused by the server.</strong> {approval.message}
        </div>
      )}
      {state === "stale" && (
        <div className="gate-note refused" role="alert">
          <strong>Approval no longer valid.</strong> The text changed after approval, or the approval
          expired. Re-read the text above and approve again.
        </div>
      )}
      {state === "approved" && (
        <div className="gate-note approved" role="status">
          <strong>Approved for matching.</strong>
        </div>
      )}
      {state === "not-enforced" && (
        <div className="gate-note warn" role="status">
          <strong>Server-side approval gate is off.</strong> {approval.message} Your review is the only
          control in effect on this server.
        </div>
      )}

      <div className="review-actions">
        <span className="muted small review-attest">
          By approving you confirm this text contains no patient identifiers.
          {enforced !== false && " Approval is recorded on the server and bound to this exact text."}
        </span>
        <button className="primary" disabled={busy} onClick={onApprove}>
          {hasResult ? "Re-approve & re-run match" : "Approve this text & match trials →"}
        </button>
      </div>
    </section>
  );
}
