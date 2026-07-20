// API client. Reads the CSRF token from the non-HttpOnly cookie set at login and
// sends it on every state-changing POST (double-submit pattern). The session cookie
// itself is HttpOnly and is sent automatically by the browser — JS never touches it.
//
// Every request is bounded: an AbortController backs both a hard timeout and a
// caller-supplied cancel signal, so a hung backend cannot leave the workspace
// stuck on "Matching…" with no way out. Errors carry a correlation ID and never
// carry request content (see lib/errors.js).

import { makeErrorId, sanitizeMessage } from "./lib/errors.js";

// Matching runs retrieval + (optionally) an LLM rerank, so it gets a much longer
// budget than the small session/health calls.
export const DEFAULT_TIMEOUT_MS = 30_000;
export const MATCH_TIMEOUT_MS = 120_000;
export const UPLOAD_TIMEOUT_MS = 60_000;

function getCsrf() {
  const m = document.cookie.match(/(?:^|;\s*)fmt_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

function abortError() {
  const err = new Error("Request cancelled.");
  err.name = "AbortError";
  err.isAbort = true;
  return err;
}

function apiError(message, { status, errorId } = {}) {
  const err = new Error(sanitizeMessage(message, "Request failed."));
  err.status = status;
  err.errorId = errorId || makeErrorId();
  return err;
}

async function request(
  path,
  { method = "GET", body, isForm = false, signal, timeoutMs = DEFAULT_TIMEOUT_MS, headers: extraHeaders } = {}
) {
  const headers = { ...(extraHeaders || {}) };
  if (!isForm) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["x-csrf-token"] = getCsrf();

  const controller = new AbortController();
  let timedOut = false;

  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const forwardAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener("abort", forwardAbort, { once: true });
  }

  let res;
  try {
    // Already cancelled before we started: do not open a connection at all.
    if (controller.signal.aborted) throw abortError();
    res = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      signal: controller.signal,
      body: isForm ? body : body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (timedOut) {
      throw apiError(`Request timed out after ${Math.round(timeoutMs / 1000)}s. The server did not respond.`);
    }
    // Caller-initiated cancel: surfaced as a distinct, non-error outcome.
    if (e && e.isAbort) throw e;
    if (controller.signal.aborted) throw abortError();
    throw apiError("Could not reach the server. Check your connection and retry.");
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener("abort", forwardAbort);
  }

  const text = await res.text();
  let data = null;
  let parsed = true;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    // A non-JSON body is unmodelled (proxy page, gateway HTML, …). It is NOT
    // surfaced: only the modelled `error` field is known not to echo input.
    parsed = false;
    data = null;
  }

  if (!res.ok) {
    const serverMessage = parsed && data && typeof data.error === "string" ? data.error : "";
    throw apiError(serverMessage || `Request failed (${res.status}).`, {
      status: res.status,
      errorId: (parsed && data && data.error_id) || undefined,
    });
  }
  return data;
}

export const api = {
  health: (opts) => request("/health", opts),

  // Capability probe: the OpenAPI document tells us which filters the running
  // server models, so the UI can offer newer filters (e.g. recruiting_only)
  // only where they take effect. Failure here is non-fatal by design.
  capabilities: (opts) => request("/openapi.json", { timeoutMs: 10_000, ...opts }),

  me: (opts) => request("/api/me", opts),
  login: (username, password, opts) =>
    request("/api/login", { ...opts, method: "POST", body: { username, password } }),
  logout: () => request("/api/logout", { method: "POST" }),
  extractText: (file, opts) => {
    const fd = new FormData();
    fd.append("file", file);
    return request("/api/extract-text", {
      timeoutMs: UPLOAD_TIMEOUT_MS,
      ...opts,
      method: "POST",
      body: fd,
      isForm: true,
    });
  },
  deidentify: (text, opts) =>
    request("/api/deidentify", { ...opts, method: "POST", body: { text } }),

  // Explicit human-approval step for egress. Returns an approval token bound to
  // this exact text. Older backends do not expose the endpoint; callers treat a
  // 404 as "gate not deployed" and proceed (see App.handleApproveAndMatch).
  approveDeid: (text, opts) =>
    request("/api/approve-deid", { ...opts, method: "POST", body: { text } }),

  match: (payload, { approvalToken, ...opts } = {}) =>
    request("/api/match", {
      timeoutMs: MATCH_TIMEOUT_MS,
      ...opts,
      method: "POST",
      body: payload,
      headers: approvalToken ? { "X-Deid-Approval": approvalToken } : undefined,
    }),
};
