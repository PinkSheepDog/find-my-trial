// Defensive normalization for TrialResult `reasons`, `cautions` and
// `contraindications`.
//
// The backend is migrating these from `list[str]` to a structured form that
// carries a verbatim evidence snippet and the source field it came from. Both
// shapes are live at once (an older deployment, a cached response, or a partial
// rollout can return either), so every renderer goes through here instead of
// assuming a shape. Anything unrecognisable degrades to its string form rather
// than throwing — a rendering bug must never take down the trial board.

function firstString(...candidates) {
  for (const c of candidates) {
    if (typeof c === "string" && c.trim()) return c.trim();
  }
  return "";
}

/**
 * Normalize one reason/caution entry.
 * Accepts: "plain string" | {text, evidence_snippet, source_field, grounded} | partials.
 * Returns: {text, evidence, source, grounded} | null when there is nothing to show.
 *
 * `grounded: false` means the claim is not backed by quotable trial prose. It
 * defaults to true when absent, because a plain string from an older server
 * carries no such warning and must not be labelled as if it did.
 */
export function normalizeNote(raw) {
  if (raw == null) return null;

  if (typeof raw === "string") {
    const text = raw.trim();
    return text ? { text, evidence: "", source: "", grounded: true } : null;
  }

  if (typeof raw === "number" || typeof raw === "boolean") {
    return { text: String(raw), evidence: "", source: "", grounded: true };
  }

  if (typeof raw !== "object") return null;

  // Tolerate several plausible key spellings so a backend rename does not blank
  // the UI. `text`/`evidence_snippet`/`source_field` are the contracted names.
  const text = firstString(raw.text, raw.reason, raw.caution, raw.label, raw.message, raw.value);
  const evidence = firstString(raw.evidence_snippet, raw.evidence, raw.snippet, raw.quote);
  const source = firstString(raw.source_field, raw.source, raw.field);

  if (!text && !evidence) return null;
  return {
    text: text || evidence,
    evidence,
    source,
    grounded: raw.grounded !== false,
  };
}

/** Normalize a list of entries, dropping empties. Non-arrays yield []. */
export function normalizeNotes(list) {
  if (!Array.isArray(list)) return [];
  return list.map(normalizeNote).filter(Boolean);
}

/** Flatten a note to one line for plain-text export, evidence included. */
export function noteToText(note) {
  if (!note) return "";
  const parts = [note.text];
  // An unverified quote is never exported as evidence — see NoteItems in TrialBoard.
  if (note.grounded === false) {
    parts.push("[UNVERIFIED — not backed by verbatim trial text]");
  } else if (note.evidence) {
    parts.push(`[evidence${note.source ? ` · ${note.source}` : ""}: "${note.evidence}"]`);
  }
  return parts.join(" ");
}

/** Convenience: a raw list straight to plain-text lines. */
export function notesToTextList(list) {
  return normalizeNotes(list).map(noteToText);
}
