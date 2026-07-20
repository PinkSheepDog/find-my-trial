import React, { useCallback, useEffect, useRef, useState } from "react";
import { useMediaQuery } from "../lib/useMediaQuery.js";
import { useFocusTrap } from "../lib/useFocusTrap.js";
import { ageLabel, corpusFreshness, corpusProvenance } from "../lib/freshness.js";

const NAV_ITEMS = [
  { href: "#intake", label: "1 · Intake" },
  { href: "#review", label: "2 · De-ID review" },
  { href: "#profile", label: "3 · Patient profile" },
  { href: "#board", label: "4 · Trial board" },
  { href: "#handoff", label: "5 · Handoff" },
];

export const DRAWER_QUERY = "(max-width: 760px)";

// The sidebar is a permanent rail on wide screens and a real slide-out drawer on
// narrow ones — a hamburger toggle, a backdrop, Escape to close, and a focus trap
// while open. It is never merely hidden: navigation stays reachable at every width.
export default function Sidebar({ user, health, onLogout }) {
  const isDrawer = useMediaQuery(DRAWER_QUERY);
  const [open, setOpen] = useState(false);
  const panelRef = useRef(null);
  const toggleRef = useRef(null);

  const close = useCallback(() => setOpen(false), []);

  // Leaving drawer mode (rotation, resize) must not strand the open state.
  useEffect(() => {
    if (!isDrawer) setOpen(false);
  }, [isDrawer]);

  // Lock background scroll while the drawer covers the page.
  useEffect(() => {
    if (!isDrawer || !open) return undefined;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [isDrawer, open]);

  useFocusTrap(panelRef, isDrawer && open, { onEscape: close, returnFocusTo: toggleRef });

  const fresh = corpusFreshness(health);

  return (
    <>
      {/* Visible only in drawer mode (CSS), but always in the DOM and in the
          accessibility tree so the control is never orphaned by a media query. */}
      <div className="mobile-bar">
        <button
          type="button"
          ref={toggleRef}
          className="drawer-toggle"
          aria-expanded={open}
          aria-controls="workspace-nav"
          aria-label={open ? "Close navigation menu" : "Open navigation menu"}
          onClick={() => setOpen((o) => !o)}
        >
          <span className="drawer-toggle-bars" aria-hidden="true">
            <span /><span /><span />
          </span>
          <span className="drawer-toggle-text">Menu</span>
        </button>
        <span className="mobile-bar-title">Find My Trial</span>
      </div>

      {isDrawer && open && (
        <div className="drawer-backdrop" data-testid="drawer-backdrop" onClick={close} aria-hidden="true" />
      )}

      <aside
        id="workspace-nav"
        ref={panelRef}
        className={`sidebar ${isDrawer ? "is-drawer" : ""} ${open ? "open" : ""}`}
        aria-label="Workspace navigation"
      >
        <div className="brand">
          <div className="brand-mark">FMT</div>
          <div>
            <div className="brand-title">Find My Trial</div>
            <div className="brand-sub">Clinical review workspace</div>
          </div>
          {/* Distinct from the toggle's label: two controls that announce
              identically are ambiguous to a screen-reader user. */}
          <button type="button" className="drawer-close" onClick={close} aria-label="Close menu panel">
            ✕
          </button>
        </div>

        <nav className="nav" aria-label="Workflow steps">
          {NAV_ITEMS.map((item) => (
            <a key={item.href} href={item.href} onClick={close}>{item.label}</a>
          ))}
        </nav>

        <CorpusStat health={health} fresh={fresh} />

        {health?.degraded_mode && (
          <div className="sidebar-note warn">
            Degraded mode: no LLM key set — deterministic extraction &amp; explanations.
          </div>
        )}
        <div className="sidebar-note">
          Decision support only. Final eligibility requires protocol and clinician review.
        </div>
        <div className="sidebar-foot">
          <span className="sidebar-user">{user}</span>
          <button type="button" className="link" onClick={onLogout}>Sign out</button>
        </div>
      </aside>
    </>
  );
}

// Corpus provenance, shown wherever the user is looking: how many trials, how
// current they are, and which normalization revision produced the index.
function CorpusStat({ health, fresh }) {
  const prov = corpusProvenance(health);
  return (
    <div className={`sidebar-stat ${fresh.stale ? "stale" : ""}`}>
      <span>Indexed trials</span>
      <strong>{health ? Number(health.trial_count || 0).toLocaleString() : "…"}</strong>
      <span className="corpus-line">Data current through</span>
      <strong className="corpus-date">{health ? fresh.label : "…"}</strong>
      {health && fresh.ageDays != null && (
        <span className="corpus-age">{ageLabel(fresh.ageDays)}</span>
      )}
      {health && fresh.normalizationVersion && (
        <span className="corpus-line">index {fresh.normalizationVersion}</span>
      )}
      {health && prov.integrityKnown && !prov.integrityVerified && (
        <span className="corpus-line corpus-unverified">provenance unverified</span>
      )}
    </div>
  );
}
