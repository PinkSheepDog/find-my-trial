
"""FastAPI application — the secure entry point.

Security posture enforced here:
  * Auth required on every data route (session guard); only /login, /health, and the
    static frontend are public.
  * Two-step intake: /api/deidentify returns scrubbed text + a redaction summary for
    HUMAN REVIEW; /api/match accepts ONLY de-identified text. Raw chart text is never
    accepted by the matching endpoint and is never persisted server-side.
  * CSRF: a double-submit token is issued at login and required on state-changing POSTs.
  * Security headers (CSP, HSTS in prod, no-sniff, frame-deny) on every response.
  * CORS limited to the configured frontend origin, credentials allowed.
  * No PHI logging; uploads are size-capped and processed in memory only.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api.schemas import (
    DeidRequest,
    DeidResponse,
    HealthResponse,
    LoginRequest,
    LoginResponse,
    MatchRequest,
    MatchResult,
)
from app.api.gate_schemas import ApproveDeidRequest, ApproveDeidResponse
from app.config import Settings, get_settings
from app.intake.deident import Deidentifier
from app.intake.extract_text import UploadRejected, extract_text, validate_upload
from app.matching.pipeline import MatchingPipeline
from app.security.auth import Session, SessionManager, UserStore
from app.security.deid_gate import ApprovalError, issue_approval, verify_approval
from app.security.deps import require_session, sign_sid, unsign_sid
from app.trials.index import TrialIndex
from app.trials.retrieve import RetrievalFilters

_CSRF_COOKIE = "fmt_csrf"
_CSRF_HEADER = "x-csrf-token"
_DEID_APPROVAL_HEADER = "X-Deid-Approval"

# Built React SPA (frontend/dist). Present in production images and after a local
# `npm run build`; absent in dev (where Vite serves the frontend and proxies /api).
_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_corpus_integrity(settings: Settings, path: Path) -> None:
    """Verify the corpus against the configured digest.

    The download URL is operator-configurable, so without this TLS is the only
    integrity control: a swapped release asset, a misconfigured URL, or a
    truncated-but-nonzero file would be indexed and served as though correct.
    An unset digest is allowed (the corpus is public, not secret) but says so
    out loud rather than implying verification happened."""
    expected = settings.trials_csv_sha256.strip().lower()
    if not expected:
        print(
            "[corpus] WARNING: FMT_TRIALS_CSV_SHA256 is not set — corpus integrity is "
            "UNVERIFIED. Whatever bytes are present will be indexed and served.",
            flush=True,
        )
        return
    actual = _sha256_file(path)
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError(
            f"Trial corpus at {path} failed integrity verification.\n"
            f"  expected sha256: {expected}\n"
            f"  actual   sha256: {actual}\n"
            "Refusing to start. Delete the file to re-fetch, or correct "
            "FMT_TRIALS_CSV_SHA256 if the corpus was intentionally updated."
        )
    print(f"[corpus] integrity verified (sha256 {actual[:16]}…)", flush=True)


def _ensure_corpus(settings: Settings) -> None:
    """Make sure the trial CSV exists locally. On a fresh cloud deploy the 33MB corpus
    is not in git; if a download URL is configured, fetch it once at startup."""
    path = Path(settings.trials_csv_path)
    if path.exists() and path.stat().st_size > 0:
        _verify_corpus_integrity(settings, path)
        return
    if not settings.trials_csv_url:
        raise RuntimeError(
            f"Trial corpus not found at {path} and FMT_TRIALS_CSV_URL is not set. "
            "Provide the corpus file or a download URL."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with httpx.stream("GET", settings.trials_csv_url, follow_redirects=True, timeout=180.0) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
    # Verify BEFORE publishing to the real path, so a corrupt download is never
    # left in place to be picked up by the next boot's existence check.
    try:
        _verify_corpus_integrity(settings, tmp)
    except RuntimeError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)  # atomic: never leave a half-written corpus in place


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Ensure the corpus is present (cloud deploys fetch it on first boot), then
    # build the trial index once at startup (content-hashed).
    _ensure_corpus(settings)
    app.state.index = TrialIndex.from_csv(settings.trials_csv_path)
    app.state.pipeline = MatchingPipeline(settings, app.state.index)
    app.state.deidentifier = Deidentifier(use_presidio=settings.use_presidio)
    app.state.sessions = SessionManager(settings.session_idle_timeout_minutes * 60)
    app.state.users = UserStore()
    # Seed an admin account if a password is configured.
    if settings.admin_password:
        app.state.users.add(settings.admin_username, settings.admin_password)
    yield
    # Nothing to persist — no patient data is held.


app = FastAPI(title="Find My Trial", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'"
    )
    if get_settings().is_production:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


def _add_cors(app: FastAPI) -> None:
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", _CSRF_HEADER, _DEID_APPROVAL_HEADER],
    )


_add_cors(app)


# --------------------------------------------------------------------------- helpers
def _require_csrf(request: Request) -> None:
    cookie = request.cookies.get(_CSRF_COOKIE)
    header = request.headers.get(_CSRF_HEADER)
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing or invalid")


def _set_auth_cookies(response: Response, settings: Settings, sid: str) -> str:
    signed = sign_sid(settings, sid)
    secure = settings.is_production
    response.set_cookie(
        settings.session_cookie_name, signed, httponly=True, samesite="strict",
        secure=secure, max_age=settings.session_idle_timeout_minutes * 60, path="/",
    )
    csrf = secrets.token_urlsafe(32)
    # CSRF cookie is readable by JS (double-submit pattern) but useless without the session.
    response.set_cookie(_CSRF_COOKIE, csrf, httponly=False, samesite="strict",
                        secure=secure, path="/")
    return csrf


# --------------------------------------------------------------------------- routes
@app.get("/health", response_model=HealthResponse)
async def health(request: Request, settings: Settings = Depends(get_settings)):
    manifest = request.app.state.index.manifest()
    return HealthResponse(
        ok=True, trial_count=manifest["row_count"],
        llm_enabled=settings.llm_enabled, degraded_mode=not settings.llm_enabled,
        data_current_through=manifest["data_current_through"],
        normalization_version=manifest["normalization_version"],
        # Build/version diagnostics: which corpus and which code produced this board.
        # Read defensively — the manifest is versioned independently of this route.
        app_version=app.version,
        corpus_content_hash=manifest.get("content_hash"),
        index_built_at=manifest.get("built_at"),
        corpus_integrity_verified=bool(settings.trials_csv_sha256.strip()),
        deid_review_enforced=settings.require_deid_review,
    )


@app.post("/api/login", response_model=LoginResponse)
async def login(payload: LoginRequest, response: Response, request: Request,
                settings: Settings = Depends(get_settings)):
    users: UserStore = request.app.state.users
    sessions: SessionManager = request.app.state.sessions
    if not users.verify(payload.username, payload.password):
        # Uniform error + no timing oracle (verify hashes even on unknown user).
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    sid = sessions.create(payload.username)
    csrf = _set_auth_cookies(response, settings, sid)
    return LoginResponse(username=payload.username, csrf_token=csrf)


@app.post("/api/logout")
async def logout(request: Request, response: Response, settings: Settings = Depends(get_settings),
                 session: Session = Depends(require_session)):
    _require_csrf(request)
    raw = request.cookies.get(settings.session_cookie_name)
    sid = unsign_sid(settings, raw) if raw else None
    request.app.state.sessions.destroy(sid)
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(_CSRF_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(session: Session = Depends(require_session)):
    return {"username": session.username}


@app.post("/api/extract-text")
async def extract_document(
    request: Request,
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(require_session),
):
    """Local document -> text. Size-capped, in-memory only, no persistence.
    Returns raw extracted text to the CLIENT (still on the user's machine via the
    local app); de-identification is a separate, explicit step before any egress."""
    _require_csrf(request)
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"File exceeds {settings.max_upload_mb} MB limit")
    try:
        validate_upload(file.filename or "", data)
    except UploadRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    doc = extract_text(file.filename or "upload", data)
    return {"text": doc.text, "source_kind": doc.source_kind,
            "warnings": doc.warnings, "ocr_used": doc.ocr_used}


@app.post("/api/deidentify", response_model=DeidResponse)
async def deidentify_endpoint(
    payload: DeidRequest, request: Request,
    session: Session = Depends(require_session),
):
    """Step 1 of egress: scrub identifiers and return the result for HUMAN REVIEW.
    No LLM call, no persistence. The raw input is discarded after this returns."""
    _require_csrf(request)
    result = request.app.state.deidentifier.deidentify(payload.text)
    return DeidResponse(
        deidentified_text=result.text, redaction_summary=result.summary(),
        redaction_counts=result.redaction_counts, total_redactions=result.total_redactions,
    )


@app.post("/api/approve-deid", response_model=ApproveDeidResponse)
async def approve_deid_endpoint(
    payload: ApproveDeidRequest, request: Request,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(require_session),
):
    """Step 2 of egress: record explicit human approval of the scrubbed text.

    This is the gate itself. It refuses to approve text that still carries
    detectable identifiers, so approving un-scrubbed text is not merely
    discouraged — it fails. The returned token binds to this exact text; matching
    different text under it is rejected."""
    _require_csrf(request)
    residual = request.app.state.deidentifier.deidentify(payload.text)
    if residual.total_redactions > 0:
        # Do not echo the text or the matched values — only counts by category.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Cannot approve: {residual.total_redactions} identifier(s) still present "
                f"({residual.summary()}). Re-run de-identification before approving."
            ),
        )
    return ApproveDeidResponse(
        approval_token=issue_approval(settings, payload.text),
        expires_in_minutes=settings.deid_approval_ttl_minutes,
        residual_redactions=0,
    )


@app.post("/api/match", response_model=MatchResult)
async def match_endpoint(
    payload: MatchRequest, request: Request,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(require_session),
    x_deid_approval: str | None = Header(default=None, alias="X-Deid-Approval"),
):
    """Step 3 of egress: run matching on APPROVED de-identified text only.

    Two independent controls, because either alone is insufficient:
      * the approval gate proves this exact text was explicitly approved, and
      * the re-scrub below means even an approved payload cannot carry PHI onward.
    A re-scrub is not a substitute for the gate: it silently rewrites the text
    rather than refusing the request, so on its own it would let an un-reviewed
    chart through in redacted form."""
    _require_csrf(request)
    if settings.require_deid_review:
        try:
            verify_approval(settings, x_deid_approval, payload.deidentified_text)
        except ApprovalError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from None
    safe_text = request.app.state.deidentifier.deidentify(payload.deidentified_text).text
    pipeline: MatchingPipeline = request.app.state.pipeline
    # Single conversion point, owned by the request model: a filter added to the API
    # cannot silently fail to reach retrieval (which is how `location` stayed dead).
    profile, match = await pipeline.run(
        safe_text, top_k=payload.top_k, filters=payload.to_retrieval_filters()
    )
    return MatchResult(profile=profile, match=match)


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    # Uniform error shape; never echo internals. The error_id is a random
    # correlation handle so a user can report a failure without pasting any
    # chart content — it carries no information about the request itself.
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "error_id": secrets.token_hex(6)},
    )


@app.exception_handler(Exception)
async def unhandled_exc_handler(request: Request, exc: Exception):
    """Catch-all so an unexpected failure mid-pipeline cannot surface a traceback
    (which may quote chart text) to the client. The detail stays server-side."""
    error_id = secrets.token_hex(6)
    # Deliberately logs the type and id only — never the message, which for a
    # parsing/extraction error can contain patient text.
    print(f"[error {error_id}] unhandled {type(exc).__name__}", flush=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "Internal error. Nothing was saved.", "error_id": error_id},
    )


# --------------------------------------------------------------------------- static SPA
# Serve the built React app from the SAME origin as the API (so the HttpOnly,
# SameSite=Strict session cookie works without cross-site relaxation). Registered
# LAST so every /api and /health route is matched first. Only mounted when a build
# exists — in dev, Vite serves the frontend and proxies /api, so this is skipped.
if _FRONTEND_DIST.is_dir():
    _INDEX_HTML = _FRONTEND_DIST / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        # Unknown API paths must 404 as JSON, not fall through to index.html.
        if full_path == "health" or full_path.startswith("api/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        candidate = (_FRONTEND_DIST / full_path).resolve()
        if _FRONTEND_DIST in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_INDEX_HTML)  # SPA fallback (client-side routing / refresh)
