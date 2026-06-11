import React from "react";

const STATUS_CLASS = {
  positive: "bio-pos",
  negative: "bio-neg",
  low: "bio-low",
  equivocal: "bio-eq",
  unknown: "bio-unk",
};

// Biomarkers are rendered WITH DIRECTION. A HER2-low or BRCA-negative marker is
// visually distinct from positive — the UI makes the old "negative-read-as-positive"
// failure impossible to overlook.
function Biomarker({ b }) {
  return (
    <span className={`chip ${STATUS_CLASS[b.status] || "bio-unk"}`} title={b.detail || ""}>
      {b.name} <em>{b.status}</em>
      {b.detail ? <span className="chip-detail"> · {b.detail}</span> : null}
    </span>
  );
}

export default function PatientProfile({ profile }) {
  const p = profile;
  return (
    <section id="profile" className="panel">
      <div className="panel-head">
        <h2>3 · Extracted Patient Profile</h2>
        <p>Structured signals from the de-identified chart. Extractor: <code>{p.extractor}</code>.</p>
      </div>

      <div className="profile-summary">{p.summary_line || summarize(p)}</div>

      <div className="profile-grid">
        <Field label="Age / Sex" value={[p.age && `${p.age}`, p.sex].filter(Boolean).join(" · ") || "—"} />
        <Field label="Stage" value={p.stage || (p.is_metastatic ? "Metastatic" : "—")} />
        <Field label="ECOG" value={p.ecog ?? "—"} />
        <Field label="Disease sites" value={p.disease_sites?.join(", ") || "—"} />
      </div>

      <Group title="Diagnosis">
        {p.cancer_types?.length ? p.cancer_types.map((c) => <span key={c} className="chip">{c}</span>) : <Empty />}
      </Group>

      <Group title="Biomarkers (with direction)">
        {p.biomarkers?.length ? p.biomarkers.map((b) => <Biomarker key={b.name} b={b} />) : <Empty />}
      </Group>

      <Group title="Therapies">
        {p.therapies?.length ? p.therapies.map((t) => (
          <span key={t.name} className={`chip ${t.caused_toxicity ? "tox" : ""}`} title={t.caused_toxicity || ""}>
            {t.name}{t.caused_toxicity ? " ⚠" : ""}
          </span>
        )) : <Empty />}
      </Group>

      <div className="profile-grid">
        <Group title="Comorbidities" inline>
          {p.comorbidities?.length ? p.comorbidities.map((c) => <span key={c} className="chip soft">{c}</span>) : <Empty />}
        </Group>
        <Group title="Organ-function flags" inline>
          {p.organ_function_flags?.length ? p.organ_function_flags.map((c) => <span key={c} className="chip soft">{c}</span>) : <Empty />}
        </Group>
      </div>

      {p.location_preferences?.length > 0 && (
        <Group title="Location preferences">
          {p.location_preferences.map((l) => <span key={l} className="chip soft">{l}</span>)}
        </Group>
      )}

      {p.missing_or_uncertain?.length > 0 && (
        <div className="uncertain">
          <strong>Missing / uncertain:</strong> {p.missing_or_uncertain.join(", ")}
        </div>
      )}
    </section>
  );
}

function Field({ label, value }) {
  return (
    <div className="stat">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
function Group({ title, children, inline }) {
  return (
    <div className={`group ${inline ? "inline" : ""}`}>
      <h3>{title}</h3>
      <div className="chips">{children}</div>
    </div>
  );
}
const Empty = () => <span className="muted small">none extracted</span>;

function summarize(p) {
  const bits = [];
  if (p.age || p.sex) bits.push([p.age, p.sex].filter(Boolean).join(" "));
  if (p.diagnosis) bits.push(p.diagnosis);
  return bits.join(" · ");
}
