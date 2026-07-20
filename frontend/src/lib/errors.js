// Error identity and message hygiene.
//
// Two rules hold everywhere an error reaches the screen:
//
//   1. Every surfaced error carries a short correlation ID, so a clinician can
//      report "FMT-3F9C21 failed" without pasting any chart content.
//   2. An error message is never allowed to carry chart text. Request bodies are
//      never interpolated into messages, and a non-JSON response body (a proxy
//      page, an HTML error, anything unmodelled) is discarded rather than shown,
//      because only a modelled `{"error": ...}` field is known not to echo input.

const MAX_MESSAGE_LEN = 300;

// Control characters, which is the shape leaked document bytes would take.
// Built from a string so no literal control character appears in this source.
const CONTROL_CHARS = new RegExp("[\\u0000-\\u001f\\u007f]+", "g");

/** Short, human-quotable correlation ID. Carries no request information. */
export function makeErrorId() {
  const bytes = new Uint8Array(3);
  if (typeof crypto !== "undefined" && crypto.getRandomValues) {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `FMT-${hex.toUpperCase()}`;
}

/**
 * Collapse a message to a single safe line: control characters stripped,
 * whitespace collapsed, length capped.
 */
export function sanitizeMessage(message, fallback = "Something went wrong.") {
  if (typeof message !== "string") return fallback;
  const flat = message.replace(CONTROL_CHARS, " ").replace(/\s+/g, " ").trim();
  if (!flat) return fallback;
  return flat.length > MAX_MESSAGE_LEN ? `${flat.slice(0, MAX_MESSAGE_LEN)}…` : flat;
}

/** Build the {message, errorId} pair the UI renders for a caught error. */
export function describeError(err, fallback = "Something went wrong.") {
  if (!err) return { message: fallback, errorId: makeErrorId(), aborted: false };
  if (err.name === "AbortError" || err.isAbort) {
    return { message: "Request cancelled.", errorId: "", aborted: true };
  }
  return {
    message: sanitizeMessage(err.message, fallback),
    // Prefer the server's correlation ID when present so both sides can be
    // grepped with one token; otherwise mint a client-side one.
    errorId: err.errorId || makeErrorId(),
    aborted: false,
  };
}
