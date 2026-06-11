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
  where status ∈ {positive, negative, low, equivocal, unknown}. "Negative-read-as-
  positive" is representationally impossible. Proven by regression tests.
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

### 1. Backend

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create the env file and a trial corpus:

```powershell
copy ..\.env.example ..\.env       # then edit ..\.env
# place a ClinicalTrials.gov-style CSV at backend/data/trials.csv
```

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
