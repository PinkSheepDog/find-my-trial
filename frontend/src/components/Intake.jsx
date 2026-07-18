import React, { useState } from "react";
import { api } from "../api.js";

const SAMPLE = `62-year-old female with metastatic HER2-positive breast cancer involving liver and bone. Prior lumpectomy, doxorubicin/cyclophosphamide, trastuzumab, paclitaxel, pertuzumab. Current fatigue, bone pain, dyspnea, weight loss. Labs include Hb 10.9, ALT 55, AST 62, creatinine 0.9. ECOG 1. Plan: evaluate HER2-targeted trials including ADCs and TKIs.`;

export default function Intake({ onDeidentify, filters, setFilters, busy }) {
  const [text, setText] = useState("");
  const [fileNote, setFileNote] = useState("");

  async function onFile(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setFileNote(`Extracting text from ${file.name}…`);
    try {
      const r = await api.extractText(file);
      setText((t) => (t ? t + "\n\n" : "") + (r.text || ""));
      const warn = r.warnings?.length ? ` (${r.warnings.join("; ")})` : "";
      setFileNote(`Loaded ${file.name} [${r.source_kind}]${warn}`);
    } catch (err) {
      setFileNote(`Could not read file: ${err.message}`);
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
          {fileNote && <div className="file-note">{fileNote}</div>}

          <textarea
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
              De-identify & review →
            </button>
          </div>
        </div>

        <aside className="intake-controls">
          <h3>Search settings</h3>
          <label>
            <span>Top results</span>
            <input type="number" min={1} max={30} value={filters.top_k}
              onChange={(e) => setFilters({ ...filters, top_k: Number(e.target.value) })} />
          </label>
          <label>
            <span>Location focus</span>
            <input type="text" placeholder="Detroit, Michigan" value={filters.location}
              onChange={(e) => setFilters({ ...filters, location: e.target.value })} />
          </label>
          <label className="check">
            <input type="checkbox" checked={filters.active_only}
              onChange={(e) => setFilters({ ...filters, active_only: e.target.checked })} />
            <span>Recruiting / active only</span>
          </label>
          <label className="check">
            <input type="checkbox" checked={filters.interventional_only}
              onChange={(e) => setFilters({ ...filters, interventional_only: e.target.checked })} />
            <span>Interventional only</span>
          </label>
        </aside>
      </div>
    </section>
  );
}
