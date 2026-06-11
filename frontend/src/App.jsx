import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import Login from "./components/Login.jsx";
import Intake from "./components/Intake.jsx";
import DeidReview from "./components/DeidReview.jsx";
import PatientProfile from "./components/PatientProfile.jsx";
import TrialBoard from "./components/TrialBoard.jsx";
import Handoff from "./components/Handoff.jsx";

// The workspace is a guarded, multi-step flow:
//   login -> intake (paste/upload) -> de-id REVIEW (human gate) -> results.
// Raw chart text never leaves the browser until the user approves the
// de-identified version on the review screen.
export default function App() {
  const [auth, setAuth] = useState({ checked: false, user: null });
  const [health, setHealth] = useState(null);

  const [rawText, setRawText] = useState("");
  const [deid, setDeid] = useState(null); // {deidentified_text, redaction_summary, ...}
  const [filters, setFilters] = useState({
    top_k: 10, active_only: true, interventional_only: false, location: "",
  });
  const [result, setResult] = useState(null); // {profile, match}
  const [shortlist, setShortlist] = useState([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
    api.me()
      .then((m) => setAuth({ checked: true, user: m.username }))
      .catch(() => setAuth({ checked: true, user: null }));
  }, []);

  if (!auth.checked) return <div className="boot">Loading workspace…</div>;
  if (!auth.user) return <Login onLogin={(u) => setAuth({ checked: true, user: u })} />;

  async function handleDeidentify(text) {
    setError("");
    setBusy("Removing identifiers for review…");
    try {
      const d = await api.deidentify(text);
      setRawText(text);
      setDeid(d);
      setResult(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function handleApproveAndMatch() {
    setError("");
    setBusy("Matching against trial corpus…");
    try {
      const r = await api.match({ deidentified_text: deid.deidentified_text, ...filters });
      setResult(r);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  function toggleShortlist(nct) {
    setShortlist((s) => (s.includes(nct) ? s.filter((x) => x !== nct) : [...s, nct]));
  }

  async function logout() {
    await api.logout().catch(() => {});
    setAuth({ checked: true, user: null });
    setRawText(""); setDeid(null); setResult(null); setShortlist([]);
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">FMT</div>
          <div>
            <div className="brand-title">Find My Trial</div>
            <div className="brand-sub">Clinical review workspace</div>
          </div>
        </div>
        <nav className="nav">
          <a href="#intake">1 · Intake</a>
          <a href="#review">2 · De-ID review</a>
          <a href="#profile">3 · Patient profile</a>
          <a href="#board">4 · Trial board</a>
          <a href="#handoff">5 · Handoff</a>
        </nav>
        <div className="sidebar-stat">
          <span>Indexed trials</span>
          <strong>{health ? health.trial_count.toLocaleString() : "…"}</strong>
        </div>
        {health?.degraded_mode && (
          <div className="sidebar-note warn">
            Degraded mode: no LLM key set — deterministic extraction & explanations.
          </div>
        )}
        <div className="sidebar-note">
          Decision support only. Final eligibility requires protocol and clinician review.
        </div>
        <div className="sidebar-foot">
          <span>{auth.user}</span>
          <button className="link" onClick={logout}>Sign out</button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <h1>Clinical Trial Review Workspace</h1>
          <p>Chart intake → de-identification review → ranked trials → handoff.</p>
        </header>

        {error && <div className="banner error">{error}</div>}
        {busy && <div className="banner busy">{busy}</div>}

        <Intake onDeidentify={handleDeidentify} filters={filters} setFilters={setFilters} busy={!!busy} />

        {deid && (
          <DeidReview
            deid={deid}
            onApprove={handleApproveAndMatch}
            onEdit={(t) => setDeid({ ...deid, deidentified_text: t })}
            busy={!!busy}
            hasResult={!!result}
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
            />
          </>
        )}
      </main>
    </div>
  );
}
