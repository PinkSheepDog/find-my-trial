import React, { useState } from "react";
import { api } from "../api.js";
import { ACTIVE_STATUSES, RECRUITING_STATUSES } from "../lib/filters.js";
import { describeError } from "../lib/errors.js";

const SAMPLE = `62-year-old female with metastatic HER2-positive breast cancer involving liver and bone. Prior lumpectomy, doxorubicin/cyclophosphamide, trastuzumab, paclitaxel, pertuzumab. Current fatigue, bone pain, dyspnea, weight loss. Labs include Hb 10.9, ALT 55, AST 62, creatinine 0.9. ECOG 1. Plan: evaluate HER2-targeted trials including ADCs and TKIs.`;

export default function Intake({ onDeidentify, filters, setFilters, busy, serverFilters }) {
  const [text, setText] = useState("");
  const [fileNote, setFileNote] = useState("");
  const [fileError, setFileError] = useState(null);

  // Offer a filter only where the server models it (null = capabilities unknown,
  // in which case we show the long-standing filters and hide the newer one).
  const NEWER_FILTERS = new Set(["recruiting_only", "location_required"]);
  const supports = (field) => (serverFilters ? serverFilters.has(field) : !NEWER_FILTERS.has(field));

  // Recruitment status and study type are each a single ordered choice, not
  // independent switches: "open to enrolment" is strictly narrower than "active",
  // and "treatment" is strictly narrower than "interventional". Presenting them as
  // separate checkboxes let a user tick contradictory-looking pairs and left the
  // effective filter ambiguous. One select per axis makes the narrowing explicit.
  const statusValue = filters.recruiting_only ? "recruiting" : (filters.active_only ? "active" : "any");
  const typeValue = filters.treatment_only ? "treatment" : (filters.interventional_only ? "interventional" : "all");

  const STATUS_HELP = {
    recruiting: `Admits ${RECRUITING_STATUSES.join(", ")}.`,
    active: `Admits ${ACTIVE_STATUSES.join(", ")}. ACTIVE_NOT_RECRUITING studies are ongoing but closed to new enrolment.`,
    any: "No status filter. Completed, terminated and withdrawn studies may appear.",
  };
  const TYPE_HELP = {
    treatment: "Treatment and expanded-access studies only.",
    interventional: "All interventional studies, including diagnostic, screening and prevention. Excludes observational.",
    all: "No study-type filter. Observational studies and registries may appear.",
  };

  function onStatusChange(e) {
    const v = e.target.value;
    setFilters({
      ...filters,
      recruiting_only: v === "recruiting",
      active_only: v === "recruiting" || v === "active",
    });
  }

  function onTypeChange(e) {
    const v = e.target.value;
    setFilters({
      ...filters,
      treatment_only: v === "treatment",
      interventional_only: v === "treatment" || v === "interventional",
    });
  }

  async function onFile(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setFileError(null);
    setFileNote(`Extracting text from ${file.name}…`);
    try {
      const r = await api.extractText(file);
      const extracted = (r.text || "").trim();
      const warn = r.warnings?.length ? r.warnings.join(" ") : "";
      if (!extracted) {
        // The file was accepted but no usable text came out (e.g. a scan with no OCR).
        // That is a dead end for the user, not a success — say so plainly and don't
        // pretend the chart is loaded.
        setFileNote("");
        setFileError({
          message: warn || `No text could be read from ${file.name}. Paste the report text below.`,
          errorId: "",
        });
        return;
      }
      setText((t) => (t ? t + "\n\n" : "") + extracted);
      // Text came through; a warning here is a caveat (e.g. one scanned page), not a failure.
      setFileNote(warn ? `Loaded ${file.name} — ${warn}` : `Loaded ${file.name}.`);
    } catch (err) {
      setFileNote("");
      setFileError(describeError(err, "Could not read that file."));
    }
  }

  return (
    <section id="intake" className="panel">
      <div className="panel-head">
        <h2>1 · Patient Intake</h2>
        <p>Paste chart text or upload a document. Nothing leaves this machine until you approve de-identification.</p>
      </div>

      <div className="intake-grid">
        <div className="intake-main">
          <label className="file-row">
            <span>Upload chart — FHIR/C-CDA, PDF, DOCX, TXT, or image</span>
            <input type="file"
              accept=".json,.fhir,.ndjson,.xml,.pdf,.docx,.txt,.rtf,.png,.jpg,.jpeg,.tif,.tiff"
              onChange={onFile} />
          </label>
          {fileNote && <div className="file-note" role="status" aria-live="polite">{fileNote}</div>}
          {fileError && (
            <div className="file-note file-error" role="alert">
              {fileError.message}
              {fileError.errorId && <> Reference <code>{fileError.errorId}</code>.</>}
            </div>
          )}

          <label className="sr-only" htmlFor="chart-text">Chart text</label>
          <textarea
            id="chart-text"
            rows={12}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Paste oncology notes, pathology, prior therapy, biomarkers, ECOG, location…"
          />

          <div className="intake-actions">
            <button className="ghost" type="button" onClick={() => setText(SAMPLE)}>Load sample chart</button>
            <button
              className="primary"
              type="button"
              disabled={busy || !text.trim()}
              onClick={() => onDeidentify(text)}
            >
              De-identify &amp; review →
            </button>
          </div>
        </div>

        <aside className="intake-controls" aria-labelledby="search-settings-heading">
          <div className="controls-head">
            <h3 id="search-settings-heading">Search settings</h3>
          </div>

          <div className="field">
            <label htmlFor="filter-topk">Results to return</label>
            <select id="filter-topk" value={filters.top_k}
              onChange={(e) => setFilters({ ...filters, top_k: Number(e.target.value) })}>
              {[5, 10, 15, 20, 25, 30].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>

          <div className="field">
            <label htmlFor="filter-location">Location focus</label>
            <input id="filter-location" type="text" placeholder="e.g. Detroit, Michigan"
              value={filters.location}
              aria-describedby="help-location"
              onChange={(e) => setFilters({ ...filters, location: e.target.value })} />
            {!supports("location_required") && (
              <p className="field-help" id="help-location">
                Location boosts ranking; it does not exclude trials.
              </p>
            )}
          </div>

          {supports("location_required") && (
            <div className="field">
              <label htmlFor="filter-location-scope">Location handling</label>
              <select id="filter-location-scope"
                value={filters.location_required ? "require" : "prefer"}
                disabled={!filters.location.trim()}
                aria-describedby="help-location"
                onChange={(e) => setFilters({ ...filters, location_required: e.target.value === "require" })}>
                <option value="prefer">Prefer sites at this location</option>
                <option value="require">Require a site at this location</option>
              </select>
              <p className="field-help" id="help-location">
                Preferring boosts ranking and flags trials with no nearby site. Requiring
                excludes them, and can legitimately return no trials.
              </p>
            </div>
          )}

          <div className="field">
            <label htmlFor="filter-status">Recruitment status</label>
            <select id="filter-status" value={statusValue} aria-describedby="help-status"
              onChange={onStatusChange}>
              {supports("recruiting_only") && <option value="recruiting">Open to enrolment only</option>}
              <option value="active">Active studies</option>
              <option value="any">Any recruitment status</option>
            </select>
            <p className="field-help" id="help-status">{STATUS_HELP[statusValue]}</p>
          </div>

          <div className="field">
            <label htmlFor="filter-type">Study type</label>
            <select id="filter-type" value={typeValue} aria-describedby="help-type"
              onChange={onTypeChange}>
              {supports("treatment_only") && <option value="treatment">Treatment studies only</option>}
              <option value="interventional">All interventional studies</option>
              <option value="all">All study types</option>
            </select>
            <p className="field-help" id="help-type">{TYPE_HELP[typeValue]}</p>
          </div>
        </aside>
      </div>
    </section>
  );
}
