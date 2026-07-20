import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import Login from "./components/Login.jsx";
import Intake from "./components/Intake.jsx";
import DeidReview from "./components/DeidReview.jsx";
import PatientProfile from "./components/PatientProfile.jsx";
import TrialBoard from "./components/TrialBoard.jsx";
import Handoff from "./components/Handoff.jsx";
import Sidebar from "./components/Sidebar.jsx";
import { DEFAULT_FILTERS, payloadForServer } from "./lib/filters.js";
import { describeError } from "./lib/errors.js";
import { corpusFreshness, corpusProvenance, ageLabel } from "./lib/freshness.js";

// The workspace is a guarded, multi-step flow:
//   login -> intake (paste/upload) -> de-id REVIEW (human gate) -> results.
// Raw chart text never leaves the browser until the user approves the
// de-identified version on the review screen.
export default function App() {
  const [auth, setAuth] = useState({ checked: false, user: null });
  const [health, setHealth] = useState(null);
  // Which MatchRequest fields the running server actually models. null = unknown.
  const [serverFilters, setServerFilters] = useState(null);

  // NOTE: the raw chart text is deliberately NOT held in app state. It was kept
  // here and never read, which is retained PHI with no purpose. It now lives only
  // in the intake textarea until de-identification returns.
  const [deid, setDeid] = useState(null); // {deidentified_text, redaction_summary, ...}
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [result, setResult] = useState(null); // {profile, match}
  const [shortlist, setShortlist] = useState([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState(null); // {message, errorId}
  // Egress-gate state: the server binds an approval token to the exact reviewed
  // text, so this records the outcome of the last explicit approval.
  const [approval, setApproval] = useState(null); // {state, message, expiresInMinutes}

  // In-flight request, so the user can cancel; and the last action, so a failed
  // one can be retried explicitly rather than by re-deriving what to click.
  const abortRef = useRef(null);
  const retryRef = useRef(null);

  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
    api.me()
      .then((m) => setAuth({ checked: true, user: m.username }))
      .catch(() => setAuth({ checked: true, user: null }));
    // Capability probe: expose only the filters this server understands.
    api.capabilities()
      .then((doc) => {
        const props = doc?.components?.schemas?.MatchRequest?.properties;
        if (props && typeof props === "object") setServerFilters(new Set(Object.keys(props)));
      })
      .catch(() => {/* unknown capabilities: send everything, server ignores extras */});
  }, []);

  const cancel = useCallback(() => {
    if (abortRef.current) abortRef.current.abort();
  }, []);

  // Every network action funnels through here so timeout, cancel, retry and
  // error-ID handling are identical no matter which button started it.
  const run = useCallback(async (label, fn) => {
    setError(null);
    setBusy(label);
    const controller = new AbortController();
    abortRef.current = controller;
    retryRef.current = { label, fn };
    try {
      await fn(controller.signal, setBusy);
    } catch (e) {
      const described = describeError(e, "The request failed.");
      if (!described.aborted) setError(described);
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setBusy("");
    }
  }, []);

  const retry = useCallback(() => {
    const last = retryRef.current;
    if (last) run(last.label, last.fn);
  }, [run]);

  if (!auth.checked) {
    return <div className="boot" role="status" aria-live="polite">Loading workspace…</div>;
  }
  if (!auth.user) return <Login onLogin={(u) => setAuth({ checked: true, user: u })} />;

  function handleDeidentify(text) {
    return run("Removing identifiers for review…", async (signal) => {
      const d = await api.deidentify(text, { signal });
      setDeid(d);
      setApproval(null);
      setResult(null);
    });
  }

  // The egress gate. Approval is an affirmative act: the reviewed text is sent to
  // /api/approve-deid, the server refuses it if identifiers remain, and the token
  // it returns is bound to that exact text. Only then does the text go to /match.
  function handleApproveAndMatch() {
    return run("Recording your approval of the reviewed text…", async (signal, setLabel) => {
      const text = deid.deidentified_text;
      setApproval(null);

      let approvalToken;
      // `deid_review_enforced: false` is a development server with the gate off.
      // Anything else (true, or an older server that omits the field) gets the
      // approval call — failing open on an unknown would defeat the gate.
      if (health?.deid_review_enforced !== false) {
        try {
          const issued = await api.approveDeid(text, { signal });
          approvalToken = issued?.approval_token;
          setApproval({
            state: "approved",
            expiresInMinutes: issued?.expires_in_minutes,
            message: "Text approved for matching.",
          });
        } catch (e) {
          if (e.status === 404) {
            // Gate not deployed on this server; the client-side review still ran.
            setApproval({ state: "not-enforced", message: "Server-side approval gate is not enabled." });
          } else if (e.status === 422) {
            // Approval REFUSED: identifiers remain. The detail carries counts by
            // category and no chart text, so it is safe to show verbatim.
            setApproval({ state: "refused", message: e.message });
            throw e;
          } else {
            throw e;
          }
        }
      } else {
        setApproval({ state: "not-enforced", message: "Server-side approval gate is disabled on this server." });
      }

      setLabel("Matching against trial corpus…");
      try {
        const r = await api.match(
          { deidentified_text: text, ...payloadForServer(filters, serverFilters) },
          { signal, approvalToken }
        );
        setResult(r);
      } catch (e) {
        if (e.status === 403) {
          // Expired, or the text no longer matches what was approved. Re-review
          // is the correct remedy — never a silent retry of a rejected egress.
          setApproval({ state: "stale", message: e.message });
          const stale = new Error(
            "Your approval is no longer valid (it expired, or the text changed after approval). " +
            "Review the de-identified text again and re-approve."
          );
          stale.status = 403;
          stale.errorId = e.errorId;
          throw stale;
        }
        throw e;
      }
    });
  }

  function toggleShortlist(nct) {
    setShortlist((s) => (s.includes(nct) ? s.filter((x) => x !== nct) : [...s, nct]));
  }

  async function logout() {
    await api.logout().catch(() => {});
    setAuth({ checked: true, user: null });
    setDeid(null); setResult(null); setShortlist([]); setApproval(null); setError(null);
  }

  const fresh = corpusFreshness(health);
  const prov = corpusProvenance(health);
  const resultCount = result?.match?.results?.length;

  return (
    <div className="app">
      <a className="skip-link" href="#main-content">Skip to main content</a>

      <Sidebar user={auth.user} health={health} onLogout={logout} />

      <main className="main" id="main-content">
        <header className="topbar">
          <h1>Clinical Trial Review Workspace</h1>
          <p>Chart intake → de-identification review → ranked trials → handoff.</p>

          {/* Corpus freshness drives referral decisions, so it sits above the
              fold rather than being fetched and discarded. */}
          <div className={`corpus-banner ${fresh.stale ? "stale" : ""}`} data-testid="corpus-banner">
            <span className="corpus-banner-label">Trial data current through</span>
            <strong className="corpus-banner-date">{health ? fresh.label : "…"}</strong>
            {health && fresh.ageDays != null && (
              <span className="corpus-banner-age">({ageLabel(fresh.ageDays)})</span>
            )}
            {health && (
              <span className="corpus-banner-meta">
                {Number(health.trial_count || 0).toLocaleString()} trials indexed
                {fresh.normalizationVersion ? ` · index ${fresh.normalizationVersion}` : ""}
              </span>
            )}
            {fresh.note && <span className="corpus-banner-note">{fresh.note}</span>}
            {/* Never imply an integrity check that did not happen. */}
            {health && prov.integrityKnown && !prov.integrityVerified && (
              <span className="corpus-banner-note">
                Corpus provenance unverified — accepted without a digest check.
              </span>
            )}
          </div>
        </header>

        {error && (
          <div className="banner error" role="alert">
            <div className="banner-text">
              <strong>Request failed.</strong> {error.message}
              {error.errorId && (
                <>
                  {" "}
                  <span className="error-id">
                    Reference <code>{error.errorId}</code> — quote this when reporting; it contains no chart content.
                  </span>
                </>
              )}
            </div>
            <div className="banner-actions">
              <button type="button" className="ghost" onClick={retry} disabled={!!busy}>Retry</button>
              <button type="button" className="link" onClick={() => setError(null)}>Dismiss</button>
            </div>
          </div>
        )}

        {busy && (
          <div className="banner busy" role="status" aria-live="polite">
            <div className="banner-text">
              <span className="spinner" aria-hidden="true" /> {busy}
            </div>
            <div className="banner-actions">
              <button type="button" className="ghost" onClick={cancel}>Cancel request</button>
            </div>
          </div>
        )}

        {/* Result-count changes are announced without stealing focus. */}
        <div className="sr-only" role="status" aria-live="polite" data-testid="result-announcer">
          {result
            ? result.match?.needs_review
              ? "Matching finished: needs review, not enough verified facts to rank trials."
              : `Matching finished: ${resultCount} trial${resultCount === 1 ? "" : "s"} ranked.`
            : ""}
        </div>

        <Intake
          onDeidentify={handleDeidentify}
          filters={filters}
          setFilters={setFilters}
          busy={!!busy}
          serverFilters={serverFilters}
        />

        {deid && (
          <DeidReview
            deid={deid}
            onApprove={handleApproveAndMatch}
            onEdit={(t) => {
              // Editing after approval invalidates the token (it is bound to a
              // digest of the approved text), so the badge is cleared here and
              // approval is re-requested on the next submit.
              setDeid({ ...deid, deidentified_text: t });
              setApproval(null);
            }}
            busy={!!busy}
            hasResult={!!result}
            approval={approval}
            enforced={health?.deid_review_enforced}
          />
        )}

        {result && (
          <>
            <PatientProfile profile={result.profile} />
            <TrialBoard
              match={result.match}
              shortlist={shortlist}
              onToggle={toggleShortlist}
            />
            <Handoff
              profile={result.profile}
              results={result.match.results}
              shortlist={shortlist}
              health={health}
              filters={filters}
            />
          </>
        )}
      </main>
    </div>
  );
}
