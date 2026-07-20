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

        <aside className="intake-controls">
          <h3>Search settings</h3>
          <label htmlFor="filter-topk">
            <span>Top results</span>
            <input id="filter-topk" type="number" min={1} max={30} value={filters.top_k}
              onChange={(e) => setFilters({ ...filters, top_k: Number(e.target.value) })} />
          </label>
          <label htmlFor="filter-location">
            <span>Location focus</span>
            <input id="filter-location" type="text" placeholder="Detroit, Michigan" value={filters.location}
              aria-describedby="help-location"
              onChange={(e) => setFilters({ ...filters, location: e.target.value })} />
          </label>
          {supports("location_required") ? (
            <>
              <label className="check">
                <input type="checkbox" checked={!!filters.location_required}
                  disabled={!filters.location.trim()}
                  aria-describedby="help-location"
                  onChange={(e) => setFilters({ ...filters, location_required: e.target.checked })} />
                <span>Require a site at this location</span>
              </label>
              <p className="filter-help" id="help-location">
                Off: location boosts ranking and adds a "no sites near here" caution.
                On: it becomes a hard filter and can legitimately return no trials.
              </p>
            </>
          ) : (
            <p className="filter-help" id="help-location">
              Location boosts ranking; it does not exclude trials.
            </p>
          )}

          <fieldset className="filter-set">
            <legend>Recruitment status</legend>

            <label className="check">
              <input type="checkbox" checked={filters.active_only}
                aria-describedby="help-active"
                onChange={(e) => setFilters({ ...filters, active_only: e.target.checked })} />
              <span>Active studies only</span>
            </label>
            {/* "Active" is broader than "recruiting" — say exactly which registry
                statuses are admitted rather than leaving the user to guess. */}
            <p className="filter-help" id="help-active">
              Includes registry status {ACTIVE_STATUSES.join(", ")}.
              ACTIVE_NOT_RECRUITING studies are ongoing but <strong>closed to new enrolment</strong>.
            </p>

            {supports("recruiting_only") && (
              <>
                <label className="check">
                  <input type="checkbox" checked={!!filters.recruiting_only}
                    aria-describedby="help-recruiting"
                    onChange={(e) => setFilters({ ...filters, recruiting_only: e.target.checked })} />
                  <span>Open to enrolment only</span>
                </label>
                <p className="filter-help" id="help-recruiting">
                  Narrows to {RECRUITING_STATUSES.join(", ")} — excludes ACTIVE_NOT_RECRUITING.
                </p>
              </>
            )}
          </fieldset>

          <fieldset className="filter-set">
            <legend>Study type</legend>

            <label className="check">
              <input type="checkbox" checked={filters.interventional_only}
                aria-describedby="help-interventional"
                onChange={(e) => setFilters({ ...filters, interventional_only: e.target.checked })} />
              <span>Interventional only</span>
            </label>
            <p className="filter-help" id="help-interventional">
              Excludes observational studies and registries.
            </p>

            {supports("treatment_only") && (
              <>
                <label className="check">
                  <input type="checkbox" checked={filters.treatment_only}
                    aria-describedby="help-treatment"
                    onChange={(e) => setFilters({ ...filters, treatment_only: e.target.checked })} />
                  <span>Treatment studies only</span>
                </label>
                <p className="filter-help" id="help-treatment">
                  On (default): only treatment and expanded-access studies. Turn off to also see
                  diagnostic, imaging, screening, prevention, supportive-care and registry studies.
                </p>
              </>
            )}
          </fieldset>
        </aside>
      </div>
    </section>
  );
}
