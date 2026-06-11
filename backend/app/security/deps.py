"""FastAPI security dependencies: signed session cookies + the require_session guard.

The cookie value is the session id signed with itsdangerous (HMAC). The signature
prevents tampering; the server-side session lookup enforces validity and timeout.
"""

from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Settings, get_settings
from app.security.auth import Session, SessionManager, UserStore

_COOKIE_SALT = "fmt-session-v1"


def get_serializer(settings: Settings = Depends(get_settings)) -> URLSafeSerializer:
    return URLSafeSerializer(settings.secret_key, salt=_COOKIE_SALT)


def sign_sid(settings: Settings, sid: str) -> str:
    return URLSafeSerializer(settings.secret_key, salt=_COOKIE_SALT).dumps(sid)


def unsign_sid(settings: Settings, signed: str) -> str | None:
    try:
        return URLSafeSerializer(settings.secret_key, salt=_COOKIE_SALT).loads(signed)
    except BadSignature:
        return None


def get_session_manager(request: Request) -> SessionManager:
    return request.app.state.sessions


def get_user_store(request: Request) -> UserStore:
    return request.app.state.users


def require_session(
    request: Request,
    settings: Settings = Depends(get_settings),
    sessions: SessionManager = Depends(get_session_manager),
) -> Session:
    """Guard for protected routes. 401 if no valid, unexpired session cookie."""
    raw = request.cookies.get(settings.session_cookie_name)
    sid = unsign_sid(settings, raw) if raw else None
    sess = sessions.get(sid)
    if sess is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return sess
