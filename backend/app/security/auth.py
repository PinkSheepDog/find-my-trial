"""Password hashing (Argon2id) + user store + server-side session manager.

The user store and session store are in-memory by design for the local single-node
deployment: NO patient data is persisted, and credentials live only for the process
lifetime unless a future datastore is wired in. The interfaces are deliberately
small so a SQLite/Postgres-backed store can replace them without touching callers.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()  # Argon2id with sane defaults


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, Exception):
        return False


@dataclass
class User:
    username: str
    password_hash: str


@dataclass
class Session:
    sid: str
    username: str
    created_at: float
    last_seen: float


class UserStore:
    """In-memory user store. Seeded with an admin account from config on startup."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    def add(self, username: str, password: str) -> None:
        self._users[username.lower()] = User(username=username, password_hash=hash_password(password))

    def get(self, username: str) -> User | None:
        return self._users.get(username.lower())

    def verify(self, username: str, password: str) -> bool:
        user = self.get(username)
        if not user:
            # Defend against username-enumeration timing by hashing anyway.
            verify_password(hash_password("dummy"), password)
            return False
        return verify_password(user.password_hash, password)

    @property
    def count(self) -> int:
        return len(self._users)


class SessionManager:
    """Server-side sessions. The cookie only carries the opaque sid; everything
    else is here. Idle timeout is enforced on every access."""

    def __init__(self, idle_timeout_seconds: int) -> None:
        self._sessions: dict[str, Session] = {}
        self._idle = idle_timeout_seconds

    def create(self, username: str) -> str:
        sid = secrets.token_urlsafe(32)
        now = time.time()
        self._sessions[sid] = Session(sid=sid, username=username, created_at=now, last_seen=now)
        return sid

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        sess = self._sessions.get(sid)
        if not sess:
            return None
        now = time.time()
        if now - sess.last_seen > self._idle:
            # Expired -> revoke.
            self._sessions.pop(sid, None)
            return None
        sess.last_seen = now  # sliding idle window
        return sess

    def destroy(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(sid, None)

    def sweep(self) -> int:
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if now - s.last_seen > self._idle]
        for sid in expired:
            self._sessions.pop(sid, None)
        return len(expired)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
