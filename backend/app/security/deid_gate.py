"""Server-side enforcement of the de-identification review gate.

The requirement is that de-identified text is reviewed by a human before it can
reach an external model. Previously `FMT_REQUIRE_DEID_REVIEW` was declared in
config and read nowhere: the gate lived entirely in React state, so any
authenticated caller could POST raw chart text straight to /api/match and skip
it. Dead safety config is worse than none, because it reads as protection.

What a stateless API can and cannot enforce, stated plainly:

  * It CANNOT prove a human read a screen. Nothing served over HTTP can.
  * It CAN enforce that the text was submitted for approval, that approval was
    an explicit separate act, that the approved text is byte-for-byte what gets
    matched, and that the approval is recent.

That is the enforceable core, and it closes the direct-POST bypass. The token is
an HMAC over a digest of the approved text — it carries no patient content, so it
is safe to hand to the client and safe if logged.

Binding to the digest is what makes it more than a rubber stamp: an attacker
cannot approve innocuous text and then match a different chart under the same
token, because the digest would not match.
"""

from __future__ import annotations

import hashlib
import hmac

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from app.config import Settings

_SALT = b"fmt-deid-approval-v1"


class ApprovalError(Exception):
    """Raised when an approval token is missing, invalid, expired, or bound to
    different text than the caller is trying to match."""


def text_digest(text: str) -> str:
    """Stable digest of approved text. Whitespace is normalized so a trailing
    newline from a textarea does not invalidate an otherwise identical approval,
    but nothing else is — any real edit produces a different digest and requires
    a fresh review."""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _signer(settings: Settings) -> TimestampSigner:
    return TimestampSigner(settings.secret_key, salt=_SALT)


def issue_approval(settings: Settings, text: str) -> str:
    """Issue a token binding this exact text as human-approved for egress."""
    return _signer(settings).sign(text_digest(text)).decode("utf-8")


def verify_approval(settings: Settings, token: str | None, text: str) -> None:
    """Raise ApprovalError unless `token` is a valid, unexpired approval for
    exactly `text`."""
    if not token:
        raise ApprovalError(
            "This text has not been approved for matching. Run de-identification "
            "and approve the scrubbed text before matching."
        )
    max_age = settings.deid_approval_ttl_minutes * 60
    try:
        signed_digest = _signer(settings).unsign(token, max_age=max_age).decode("utf-8")
    except SignatureExpired:
        raise ApprovalError(
            "The de-identification approval has expired. Review the scrubbed text again."
        ) from None
    except BadSignature:
        raise ApprovalError("The de-identification approval token is not valid.") from None

    # Constant-time: the digest is not secret, but this keeps the comparison
    # uniform with the rest of the auth surface.
    if not hmac.compare_digest(signed_digest, text_digest(text)):
        raise ApprovalError(
            "The submitted text does not match the text that was approved. "
            "Re-review the scrubbed text before matching."
        )
