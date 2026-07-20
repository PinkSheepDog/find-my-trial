# Find My Trial

A clinical-trial matching **decision-support** workspace. A clinician pastes (or
uploads) a messy patient chart; the system de-identifies it locally, extracts a
structured patient profile, retrieves and ranks plausible trials from a
ClinicalTrials.gov-style corpus, and presents an explainable shortlist with
reasons, cautions, and contraindication warnings.

> **Decision support, not eligibility determination.** Final eligibility always
> requires protocol and clinician review. Confidence scores reflect *fit*, not
> eligibility.

---

## Why this rebuild exists

A prior prototype had a fatal clinical bug: it read **"BRCA negative"** and
**"HER2 IHC 1+"** (HER2-*low*) as *positive*, matching patients to contraindicated
trials, and it inflated confidence scores it could never actually compute. It also
committed real patient PHI and a 33 MB CSV into git. This rebuild fixes all of that
by design:

- **Biomarker direction is first-class.** A biomarker is a `(name, status)` pair
  where status ∈ {positive, negative, low, equivocal, unknown}, so a direction can
  never be *dropped* or left implicit. Note the limit of that guarantee: the type
  prevents ambiguous *representation*, it does not prevent an extractor assigning a
  confidently wrong status — that is a matter of extraction rules and the tests that
  hold them honest. See "Known limitations".
- **HIPAA-conscious by construction.** Patient text is de-identified *before* any
  external call, a human reviews the scrubbed text before egress, nothing patient-
  related is persisted, and `/api/match` re-scrubs as defense-in-depth.
- **Measurable from commit one.** A golden acceptance harness runs both worked
  examples end-to-end against the real 10k-row corpus.

---

## Architecture

```
upload/paste ─► extract text (local: PDF/DOCX/TXT, OCR for scans)
            ─► DE-IDENTIFY (local rules; optional Presidio NER)
            ─► HUMAN REVIEW of scrubbed text  ◄── the egress gate
            ─► extract PatientProfile (LLM via OpenRouter, or rules fallback)
            ─► retrieve candidates (structured filters + BM25, no LLM)
            ─► rerank + explain (LLM, or deterministic fallback)
            ─► ranked trial board ─► shortlist ─► handoff
```

- **Model-agnostic.** LLM access is via OpenRouter; models are config strings
  (`anthropic/claude-sonnet-4.6`, `openai/gpt-4o`, …). Zero-Data-Retention routing
  is enforced by default.
- **Graceful degradation.** With no API key the system runs fully offline in
  *degraded mode*: deterministic negation-aware extraction and a transparent
  rule-based reranker. Every feature still works; only the LLM's richer reasoning
  is absent.
- **The only egress point** is the OpenRouter call, and only de-identified text
  ever reaches it.

---

## Quickstart (local)

From a clean checkout, on macOS/Linux, the Makefile is the shortest path:

```bash
make setup      # create backend/.venv and install pinned deps
make corpus     # download + CHECKSUM-VERIFY the trial corpus
cp .env.example .env   # then edit .env (see required values below)
make test       # full suite incl. the benchmark release gate
make run        # API on http://127.0.0.1:8000
make frontend   # build the React app (or `cd frontend && npm run dev`)
```

`make corpus` fetches the public corpus from the `corpus-v1` GitHub Release and
verifies its SHA-256 before installing it, so a swapped or truncated asset fails
loudly instead of being silently indexed.

<details>
<summary>Windows / PowerShell equivalents</summary>

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy ..\.env.example ..\.env       # then edit ..\.env
# download the corpus URL from the Makefile's CORPUS_URL to backend/data/trials.csv
```
</details>

Set at minimum in `.env`:
- `FMT_SECRET_KEY` — a long random string (`python -c "import secrets; print(secrets.token_urlsafe(64))"`)
- `FMT_ADMIN_PASSWORD` — your login password (the admin account is seeded on first run)
- `FMT_OPENROUTER_API_KEY` — optional; omit to run in degraded mode

Run:

```powershell
uvicorn app.main:app --reload --port 8000
```

### 2. Frontend

```powershell
cd frontend
npm install
npm run dev            # http://localhost:5173 (proxies /api to :8000)
```

Open http://localhost:5173, sign in, paste a chart (or "Load sample chart"),
de-identify, review, and match.

### 3. Tests

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
```

---

## Optional capabilities

These degrade gracefully if absent — the core system never depends on them.

- **Scanned-document OCR** (photos / image-only PDFs):
  ```
  pip install pytesseract pillow
  ```
  plus the Tesseract binary on PATH. Without it, digital PDFs/DOCX/TXT still work;
  scans return a clear "install OCR" warning.

- **NER de-identification** (catches free-text names the rules miss):
  ```
  pip install presidio-analyzer presidio-anonymizer
  python -m spacy download en_core_web_lg
  ```
  then set `FMT_USE_PRESIDIO=true`. The always-on rule layer covers the structured
  HIPAA identifiers regardless.

---

## Security & HIPAA posture

| Property | How it's enforced |
|---|---|
| No PHI persistence | Stateless pipeline; in-memory only; no chart written to disk/db |
| De-id before egress | Local de-id + human review gate; `/api/match` re-scrubs (defense-in-depth) |
| Single egress point | Only the OpenRouter call; ZDR routing enforced |
| Secrets | All from gitignored `.env`; nothing hardcoded; never logged |
| Auth | Argon2id, server-side sessions, HttpOnly + SameSite=Strict cookies, idle timeout |
| CSRF | Double-submit token, constant-time compare, on all state-changing POSTs |
| Transport headers | CSP, X-Frame-Options DENY, nosniff, HSTS (prod) |
| Uploads | Size-capped, in-memory, validated |

**Before any non-local deployment:** set a strong `FMT_SECRET_KEY` and admin
password, set `FMT_ENV=production` (enables HSTS), serve over HTTPS, and — because
this becomes hosted multi-user software handling PHI — execute BAAs with your LLM
provider and add audit logging and a persistent user store. The local build is
safe and responsible for development and demos with de-identified data; full
hospital-grade HIPAA certification is a deliberate, larger step beyond this scope.

---

## Known limitations

Stated plainly, because the failure mode of a decision-support tool is a user
trusting it further than it has earned.

**The trial corpus is a 10,000-row sample, not the registry.** ClinicalTrials.gov
holds roughly 555,000 studies; this ships ~1.8% of them. A genuinely suitable trial
that is not in the sample cannot be retrieved, and the board gives no indication
that it is missing. Absence of a match here is *not* evidence that no trial exists.

**Retrieval is lexical, not semantic.** Candidate generation is BM25 plus structured
gates; there is no vector index. A trial phrased in vocabulary disjoint from the
chart will not surface, however clinically relevant it is. The LLM re-ranks and
explains within the gated candidate set — it cannot recover a trial retrieval missed.

**De-identification is redaction assistance, not certified de-identification.** It is
neither Safe Harbor nor Expert Determination certified. The rule layer is pattern-
and label-driven, so it is strongest on structured identifiers (labelled fields,
dates, ZIP, email, URL, phone, SSN/MRN) and weakest on free text. Human review of the
scrubbed text is a required step, not a formality — see `docs/PRIVACY_DATA_FLOW.md`
for the current per-class coverage and residual gaps.

**Match scores are fit, not eligibility.** The score reflects how well a trial matches
the extracted profile. It is not a probability of enrolment and carries no protocol
review. Final eligibility always requires the protocol and a clinician.

**Recruiting status is only as fresh as the corpus.** The board shows the corpus's
data-current-through date; sites and statuses drift continuously between corpus
updates. Confirm status with the site before acting.

**No BAA.** Zero-Data-Retention routing is *requested* of OpenRouter, but no business
associate agreement is in place and the response is not verified to have come from a
ZDR provider. Do not run real PHI through the LLM path on this basis alone.

---

## Project layout

```
backend/
  app/
    config.py            # pydantic-settings; all secrets via .env
    intake/              # text extraction, OCR, de-identification
    extraction/          # PatientProfile schema, rules + LLM extractors
    llm/                 # OpenRouter client (model-agnostic, ZDR)
    trials/              # corpus index, structured filters + BM25 retrieval
    matching/            # rerankers (LLM + deterministic), pipeline, results
    security/            # Argon2 auth, sessions, FastAPI guards
    api/                 # request/response schemas
    main.py              # FastAPI app, middleware, routes
  tests/                 # de-id, negation regression, retrieval, golden, API
  fixtures/              # expected_outputs.json (PHI charts are gitignored)
frontend/
  src/                   # React workspace (intake → review → board → handoff)
```
