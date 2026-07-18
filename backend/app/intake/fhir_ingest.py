"""FHIR R4 / C-CDA ingestion — the 'preferred' and 'strong' input tiers.

The corpus's primary format is FHIR R4 *document* Bundles (Composition first). Rather
than build a second extraction path, we render a Bundle (or NDJSON resource stream, or
a C-CDA document) into a clinically-ordered text summary that flows through the existing
de-identify -> extract pipeline. Structured facts (Condition, Observation, Medication,
Procedure, DiagnosticReport) are laid out so the negation-aware extractor recovers
disease family, biomarker direction, ECOG, therapy timeline, and metastatic sites.

Full per-resource provenance preservation is a separate, larger step; this delivers the
ingestion capability and reuses the audited pipeline. Patient name is intentionally NOT
rendered (no clinical value; de-id would scrub it anyway).
"""

from __future__ import annotations

import datetime
import json
from xml.etree import ElementTree

# Observation code.text values treated as biomarkers (vs generic labs).
_BIOMARKER_NAMES = {
    "her2", "erbb2", "er", "estrogen receptor", "pr", "progesterone receptor",
    "egfr", "alk", "ros1", "braf", "kras", "brca", "brca1", "brca2", "pd-l1", "pdl1",
    "msi", "msi/mmr", "mmr", "hrd", "ntrk", "germline testing",
}


def is_fhir_json(data: bytes) -> bool:
    head = data.lstrip()[:400].lower()
    return b'"resourcetype"' in head


def render_fhir(data: bytes) -> tuple[str, list[str]]:
    """Render FHIR JSON (a document Bundle, a single resource, or NDJSON) to text."""
    warnings: list[str] = []
    resources = _load_resources(data, warnings)
    if not resources:
        return "", warnings or ["No FHIR resources found."]
    by_type: dict[str, list[dict]] = {}
    for r in resources:
        by_type.setdefault(r.get("resourceType", "?"), []).append(r)
    return _render(by_type), warnings


def _load_resources(data: bytes, warnings: list[str]) -> list[dict]:
    text = data.decode("utf-8", errors="ignore").strip()
    # NDJSON: one JSON object per line.
    if text and "\n" in text and not text.lstrip().startswith(("{", "[")) is False and _looks_ndjson(text):
        out = []
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                warnings.append(f"NDJSON line {i} did not parse; skipped.")
        if out:
            return out
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        warnings.append(f"FHIR JSON did not parse: {exc}")
        return []
    if isinstance(obj, dict) and obj.get("resourceType") == "Bundle":
        return [e["resource"] for e in obj.get("entry", []) if isinstance(e, dict) and "resource" in e]
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        return [obj]
    return []


def _looks_ndjson(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    return len(lines) > 1 and all(l.strip().startswith("{") for l in lines[:3])


def _render(by_type: dict[str, list[dict]]) -> str:
    lines: list[str] = ["STRUCTURED EHR INTAKE (FHIR) — SYNTHETIC/NOT FOR CLINICAL USE"]

    for p in by_type.get("Patient", []):
        bits = []
        if p.get("gender"):
            bits.append(p["gender"])
        dob = p.get("birthDate")
        if dob:
            age = _age_from_dob(dob)
            if age is not None:
                bits.append(f"age {age} years")
            bits.append(f"DOB {dob}")
        for addr in p.get("address", []):
            if addr.get("state"):
                bits.append(f"State: {addr['state']}")
        if bits:
            lines.append("Patient: " + ", ".join(bits))

    for sr in by_type.get("ServiceRequest", []):
        q = sr.get("patientInstruction") or _text(sr.get("code"))
        if q:
            lines.append("Referral question: " + q)

    for c in by_type.get("Condition", []):
        dx = _text(c.get("code"))
        status = _text(c.get("clinicalStatus"))
        stage = "; ".join(_text(s.get("summary")) for s in c.get("stage", []) if _text(s.get("summary")))
        sites = ", ".join(_text(b) for b in c.get("bodySite", []) if _text(b))
        notes = " ".join(n.get("text", "") for n in c.get("note", []))
        seg = f"Diagnosis: {dx}"
        if status:
            seg += f" [{status}]"
        if stage:
            seg += f"; {stage}"
        if sites:
            seg += f"; sites: {sites}"
        if notes:
            seg += f"; {notes}"
        lines.append(seg)

    biomarkers, labs, performance = [], [], []
    for o in by_type.get("Observation", []):
        name = _text(o.get("code"))
        val = _obs_value(o)
        date = o.get("effectiveDateTime", "")
        note = " ".join(n.get("text", "") for n in o.get("note", []))
        entry = f"{name} {val}".strip()
        if date:
            entry += f" ({date}{'; ' + note if note else ''})"
        elif note:
            entry += f" ({note})"
        low = name.lower()
        if "ecog" in low or "performance" in low:
            performance.append(f"ECOG {val}".strip())
        elif low in _BIOMARKER_NAMES:
            biomarkers.append(entry)
        else:
            labs.append(entry)
    if biomarkers:
        lines.append("Biomarkers: " + "; ".join(biomarkers))
    if performance:
        lines.append("Performance status: " + "; ".join(performance))
    if labs:
        lines.append("Labs: " + "; ".join(labs))

    for dr in by_type.get("DiagnosticReport", []):
        concl = dr.get("conclusion") or ""
        if concl:
            lines.append(f"Imaging/Pathology: {concl}")

    treatments = []
    for pr in by_type.get("Procedure", []):
        treatments.append(_treatment_line(_text(pr.get("code")), pr.get("performedPeriod"),
                                          pr.get("status"), pr.get("note")))
    for m in by_type.get("MedicationAdministration", []) + by_type.get("MedicationStatement", []):
        med = _text(m.get("medicationCodeableConcept"))
        treatments.append(_treatment_line(med, m.get("effectivePeriod"), m.get("status"), m.get("note")))
    treatments = [t for t in treatments if t]
    if treatments:
        lines.append("Treatment history:")
        lines.extend("  - " + t for t in treatments)

    return "\n".join(lines) + "\n"


def _treatment_line(name, period, status, note) -> str:
    if not name:
        return ""
    period = period or {}
    span = " to ".join(x for x in [period.get("start"), period.get("end")] if x)
    note_txt = " ".join(n.get("text", "") for n in (note or []))
    parts = [name]
    if span:
        parts.append(f"({span})")
    if status:
        parts.append(f"[{status}]")
    if note_txt:
        parts.append(note_txt)
    return " ".join(parts)


def _text(node) -> str:
    """Best-effort human text of a FHIR CodeableConcept / element."""
    if not isinstance(node, dict):
        return ""
    if node.get("text"):
        return node["text"]
    for coding in node.get("coding", []):
        if coding.get("display"):
            return coding["display"]
        if coding.get("code"):
            return coding["code"]
    return ""


def _obs_value(o: dict) -> str:
    if "valueString" in o:
        return str(o["valueString"])
    if "valueInteger" in o:
        return str(o["valueInteger"])
    if "valueQuantity" in o:
        q = o["valueQuantity"]
        return f"{q.get('value', '')} {q.get('unit', '')}".strip()
    if "valueCodeableConcept" in o:
        return _text(o["valueCodeableConcept"])
    return ""


def _age_from_dob(dob: str) -> int | None:
    try:
        y, m, d = (int(x) for x in dob[:10].split("-"))
        today = datetime.date.today()
        return today.year - y - ((today.month, today.day) < (m, d))
    except (ValueError, TypeError):
        return None


def render_ccda(data: bytes) -> tuple[str, list[str]]:
    """Best-effort narrative extraction from a C-CDA/CCD XML document (section titles
    and their human-readable <text> blocks)."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        return "", [f"C-CDA XML did not parse: {exc}"]
    out: list[str] = ["STRUCTURED EHR INTAKE (C-CDA) — SYNTHETIC/NOT FOR CLINICAL USE"]
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]  # strip namespace
        if tag in {"title", "text", "td", "paragraph", "item", "caption", "content"}:
            txt = " ".join(t.strip() for t in el.itertext() if t and t.strip())
            if txt:
                out.append(txt)
    # dedupe consecutive repeats (narrative + table often duplicate)
    deduped: list[str] = []
    for line in out:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return ("\n".join(deduped) + "\n", []) if len(deduped) > 1 else ("", ["No narrative sections found in C-CDA."])
