# Privacy & PHI data flow

Truthful account of where chart data goes, what is logged or cached, what leaves the
machine, and when each copy is discarded (feedback P0 #3). This describes the current
build honestly — including its limits. It is **not** a compliance certification.

## Where raw chart data enters

| Endpoint | Input | What happens | Persisted? |
|---|---|---|---|
| `POST /api/extract-text` | uploaded file (PDF/DOCX/TXT/RTF/FHIR/C-CDA/image) | validated (type + signature + size), parsed to text **in memory**, returned to the caller | No |
| `POST /api/deidentify` | raw chart text | de-identified locally (rules; optional Presidio), returns scrubbed text + redaction **counts** | No |
| `POST /api/match` | **de-identified text only** | re-scrubbed (defense-in-depth), extracted, matched, ranked | No |

Raw chart text is **never accepted by `/api/match`**. Each request is stateless: the
input is processed and discarded when the response returns. Nothing patient-related is
written to disk or a database — the user store and session store are in-memory only,
and the app holds no patient state between calls.

## The single egress point

The **only** place chart-derived data leaves the machine is the OpenRouter LLM call
(extraction + rerank). **Only de-identified text reaches it** — enforced by the pipeline
(`/api/match` re-scrubs before the pipeline runs) and by sending the LLM the normalized,
already-scrubbed profile. Zero-Data-Retention routing is requested by default
(`FMT_OPENROUTER_ENFORCE_ZDR=true`). With no API key the system runs fully offline
(degraded mode) and there is **no egress at all**.

## What is logged / cached

- **No chart text is logged.** Redaction **counts** (not values) are the only chart-derived
  data considered safe to log for monitoring.
- **Client errors are uniform and carry no chart content** (safe messages + status codes).
- **No response/prompt is logged** by the OpenRouter client.
- The only cache is the in-memory trial index (public trial data, no PHI), keyed by CSV
  content hash + normalization version.

## De-identification: what the code actually does

The rule layer is **redaction assistance**. It is **not** certified de-identification —
neither Safe Harbor nor Expert Determination. It is always on, needs no model and no
network, and is exercised by an adversarial suite (`tests/test_deident_adversarial.py`)
that tests both directions: identifiers must go, and clinical content must stay.

Every row below corresponds to a rule in `app/intake/deident.py` and to tests. Where a
row has a limit, the limit is stated, not glossed.

| Identifier class | Tag | What is detected | What is **not** |
|---|---|---|---|
| Names (patient, clinician, relatives) | `[NAME]` | `Name:`/`Patient Name:` labels; honorifics (`Dr./Mr./Mrs./Ms./Prof.`); clinical roles (`Nurse`, `Attending`, `Surgeon`, `Oncologist`, `Technologist`, `RN/NP/PA/APRN/CRNA/LPN/PharmD`); the colon-less `Patient <Name>` / `pt <Name>`; signature blocks (`Electronically signed by`, `/s/`, `Dictated by`); relatives and contacts (`Mother: X`, `Daughter Emily Watson`, `Next of kin - X`); trailing credentials (`A. Okafor MD`); and bare `First M. Last` / `Firstname Lastname` where the first token is a known given name | An unlabeled name with an uncommon given name, no middle initial and no role/label context. Also, a surname that collides with a clinical word (`Nurse Sample`) is deliberately kept — the word guard prevents `Attending Physician` from being read as a surname |
| Facilities / institutions | `[FACILITY]` | Capitalized run ending in an institution keyword: `Hospital`, `Clinic`, `Medical/Cancer/Health/Oncology/Surgery/Imaging/Infusion/Treatment Center`, `Health System`, `Institute`, `Infirmary`, `Hospice`, `Laboratories`, `Nursing Home` | A facility named without any such keyword (`Karmanos` alone) |
| Cities / towns | `[ADDRESS]` | `City, State`; a ~300-entry gazetteer of the largest US cities matched on sight; **any** capitalized place inside a locational phrase (`lives in`, `resides in`, `relocated to`, `travels from`, `commutes from`, `transferred from`, `from X for treatment/care/infusions`) | A city that is off-gazetteer, has no state, and appears in no locational phrase. Ambiguous names (`Mobile`, `Buffalo`, `Corona`, `Jackson`, `Independence`, `Reading`, `Washington`) are matched **only** in locational context, because redacting them on sight destroys clinical text |
| States | *(kept)* | — | Deliberately retained: Safe Harbor permits state-level geography, and the matcher needs it for site/radius preferences |
| Street address | `[ADDRESS]` | `<number> <words> <Street/Ave/Rd/Blvd/Dr/Ln/Ct/...>`, single line, Title-Cased suffix | An all-lowercase street line. The suffix is case-sensitive on purpose (see "Over-redaction" below) |
| ZIP | `[ADDRESS]` | ZIP+4 anywhere; a 5-digit ZIP after a state abbreviation, after a `ZIP`/`postal` label, or immediately after a redacted place | A bare, context-free 5-digit number — see "Known limitations" |
| Dates | `[DATE]` | `DOB:` lines, numeric dates, month-year (`March 2014`), full text dates (`May 12, 2026`, `12 August 2026`) | A bare year (`2014`) — a year alone is permitted under Safe Harbor |
| Ages > 89 | `[AGE>89]` | Unit forms (`92 year old`, `95-year-old`, `91 yo`, `93 yrs`), labeled (`Age: 93`), and copula (`The patient is 95`) | Ages **≤ 89 are intentionally retained** — they are required for trial age-eligibility matching |
| MRN / HRN | `[MRN]` | Labeled `MRN`/`HRN`/`Medical Record Number`/`Record #`, numeric or alphanumeric, value may sit on the next line. A labeled MRN takes precedence over the SSN shape | An unlabeled internal record number in an unrecognized format |
| SSN | `[SSN]` | `NNN-NN-NNNN` | — |
| Phone / fax | `[PHONE]` | NANP forms, plus labeled non-standard forms (`Phone: (555) 0100`) | An unlabeled, non-standard number |
| Email, URLs | `[EMAIL]`, `[URL]` | Addresses; `http(s)://` and bare `www.` links | — |
| Record IDs (incl. copy-forward) | `[ID]` | `Patient ID`/`Acct` labels, `P-1001`, bare digit runs ≥ 9, and site IDs of the form `SYNTH-LUNG-003` (≥ 3 all-caps hyphen segments) or `BCBS-772819` (caps + ≥ 4 digits). Repeats in header **and** footer are each redacted | Public `NCT…` trial IDs are explicitly **preserved** — they are not PHI |
| Insurance / plan / device | `[ID]` | Contextual on `policy`, `member`, `group`, `subscriber`, `insurance`, `payer`, `plan`, `serial`, `device`, `implant`, `lot`, `catalog` + an adjacent identifier token | A bare identifier with no such keyword and no ID-like shape |

### Optional NER layer

With `FMT_USE_PRESIDIO=true`, a Presidio pass runs **after** the rules and requests
`PERSON`, `LOCATION` and `ORGANIZATION`, mapped to `[NAME]`, `[ADDRESS]` and
`[FACILITY]`. (It previously requested `PERSON` only, which meant the layer this
document advertised as the mitigation for free-text places could not have caught one.)
Spans below 0.6 confidence are dropped, and a span overlapping an already-emitted tag is
never rewritten. Presidio is **optional and not installed by default**: if the import
fails or the analyzer raises, the always-on rule layer output is returned unchanged. The
code path is unit-tested with a stub analyzer, so it is covered even without Presidio
present.

### Over-redaction is treated as a bug of equal severity

Destroying clinical meaning is a real failure mode, not "extra safety". The rules are
built to fail toward keeping medicine, and this is tested:

- Hyphenated clinical tokens (`PD-L1`, `T-DM1`, `5-FU`, `CTLA-4`, `COVID-19`,
  `nab-paclitaxel`, `R-CHOP`) are structurally unreachable by the record-ID rules.
- City names that double as English, anatomy or devices are context-gated, so
  "patient is mobile", "Buffalo hump", "Corona radiata", "Jackson-Pratt drain",
  "Sister Mary Joseph nodule" survive intact.
- Two pre-existing over-redaction bugs were fixed here: the case-insensitive
  city/state rule matched `"…recommend treatment, or predict…"` as `<City>, OR`
  (Oregon) and deleted the word *treatment*; and the newline-crossing street rule
  matched `"2026 lung core.\n\nCT"` as `<number> <words> Ct` and deleted the *CT scan*.
- The deterministic benchmark (`benchmark/run_benchmark.py`, 16/16) runs extraction on
  **de-identified** text, so any regression that eats clinical signal fails CI.

## Known limitations (decisions, not oversights)

- **A bare 5-digit number is not treated as a ZIP.** 5-digit numbers collide with lab
  values, doses, accession fragments and platelet counts, and blanket-redacting them
  would corrupt charts more often than it would protect anyone. ZIPs are caught when
  they carry context (ZIP+4, a `ZIP`/`postal` label, after a state, or after an already
  redacted place). `"The code is 48226"` is therefore **not** redacted. This is
  asserted in `TestDocumentedLimitations` so it cannot change silently.
- **The city gazetteer is finite.** ~300 US cities plus locational phrasing; smaller
  municipalities and non-US cities outside a locational phrase pass through. Presidio's
  `LOCATION` entity is the mitigation.
- **Unlabeled, uncommon person names pass through in rules-only mode.** Name detection is
  positive-signal by design; a denylist-free approach is what keeps Title-Case medical
  text intact. Presidio's `PERSON` entity is the mitigation.
- **Lowercase street lines are missed.** The street suffix is matched case-sensitively
  because a case-insensitive match destroyed clinical text (see above).
- **No free-text inference.** Nothing here reasons about re-identification risk from
  combinations of quasi-identifiers (rare diagnosis + state + age), which is precisely
  what Expert Determination exists to assess.
- **This is neither Safe Harbor nor Expert Determination certified**, and no claim of
  HIPAA compliance is made.

## Deletion

Stateless: raw input exists only for the duration of one request and is released when the
response is returned. No chart, OCR output, or de-id artifact is stored. A process restart
clears the in-memory session/user stores.

## Before any real-PHI / non-local deployment

- Execute a **BAA** with the LLM provider (and confirm ZDR contractually).
- Add **audit logging** and a **persistent user/session store**.
- Serve over **HTTPS** with `FMT_ENV=production` (HSTS + Secure cookies).
- Prefer sending the LLM only **minimum-necessary normalized facts**, not free-text notes.
