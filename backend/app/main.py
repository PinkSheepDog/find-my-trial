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

import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.schemas import (
    DeidRequest,
    DeidResponse,
    HealthResponse,
    LoginRequest,
    LoginResponse,
    MatchRequest,
    MatchResult,
)
from app.config import Settings, get_settings
from app.intake.deident import Deidentifier
from app.intake.extract_text import extract_text
from app.matching.pipeline import MatchingPipeline
from app.security.auth import Session, SessionManager, UserStore
from app.security.deps import require_session, sign_sid, unsign_sid
from app.trials.index import TrialIndex
from app.trials.retrieve import RetrievalFilters

_CSRF_COOKIE = "fmt_csrf"
_CSRF_HEADER = "x-csrf-token"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Build the trial index once at startup (content-hashed).
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
        allow_headers=["Content-Type", _CSRF_HEADER],
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
    return HealthResponse(
        ok=True, trial_count=request.app.state.index.stats()["trial_count"],
        llm_enabled=settings.llm_enabled, degraded_mode=not settings.llm_enabled,
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


@app.post("/api/match", response_model=MatchResult)
async def match_endpoint(
    payload: MatchRequest, request: Request,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(require_session),
):
    """Step 2 of egress: run matching on APPROVED de-identified text only.

    Defense-in-depth: even though the client is expected to send de-identified text,
    we re-run de-identification here so the LLM can never receive un-scrubbed PHI."""
    _require_csrf(request)
    safe_text = request.app.state.deidentifier.deidentify(payload.deidentified_text).text
    pipeline: MatchingPipeline = request.app.state.pipeline
    filters = RetrievalFilters(
        active_only=payload.active_only,
        interventional_only=payload.interventional_only,
        location=payload.location,
    )
    profile, match = await pipeline.run(safe_text, top_k=payload.top_k, filters=filters)
    return MatchResult(profile=profile, match=match)


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    # Uniform error shape; never echo internals.
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
