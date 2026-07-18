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

## De-identification limits (do not over-claim)

The rule layer is **redaction assistance**, not certified de-identification. It removes the
structured HIPAA identifiers (names via label/title/given-name signal, MRN/HRN, SSN,
phone, email, URL, dates incl. month-year and full text dates, ZIP/ZIP+4, street
addresses, city+state, ages > 89, long numeric IDs) and is exercised by an **adversarial
test suite** (`tests/test_deident_adversarial.py`). Known gaps, kept visible (not hidden):

- **Bare city names without a state** and **unusual unlabeled person names** need the
  optional Presidio NER layer (`FMT_USE_PRESIDIO=true`). One such gap is a documented
  `xfail`, not a silent pass.
- This is neither Safe Harbor nor Expert Determination certified.

## Deletion

Stateless: raw input exists only for the duration of one request and is released when the
response is returned. No chart, OCR output, or de-id artifact is stored. A process restart
clears the in-memory session/user stores.

## Before any real-PHI / non-local deployment

- Execute a **BAA** with the LLM provider (and confirm ZDR contractually).
- Add **audit logging** and a **persistent user/session store**.
- Serve over **HTTPS** with `FMT_ENV=production` (HSTS + Secure cookies).
- Prefer sending the LLM only **minimum-necessary normalized facts**, not free-text notes.
