"""Authentication and session security.

Design (per the agreed plan):
  * Passwords hashed with Argon2id (never stored or logged in plaintext).
  * Server-side sessions: the cookie holds only a signed opaque session id; all
    state lives server-side so logout/timeout revokes instantly.
  * Cookies are HttpOnly + SameSite=Strict so JS (and XSS) cannot read them.
  * Idle timeout enforced server-side.
  * Every API route except login/health requires a valid session.
"""
