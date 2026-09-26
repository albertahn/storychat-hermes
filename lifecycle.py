"""Connection lifecycle rules (design spec §9.4): what each close code or upgrade status means,
reconnect backoff, and a websockets client that never follows redirects."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from websockets.asyncio.client import connect as _ws_connect

BACKOFF_START_S = 1.0
BACKOFF_RATE_LIMITED_S = 60.0
BACKOFF_MAX_S = 60.0
# Consecutive failed reconnects before handing the adapter to Hermes' reconnect watcher
# (spec §9.4 "last resort": _set_fatal_error(retryable=True) + _notify_fatal_error()).
MAX_RECONNECT_ATTEMPTS = 10

REPLACED_MSG = "another client took over — rotate your token"
TOKEN_MSG = "token invalid or revoked — generate a new one at storychat.app/chat/hermes"
PROTOCOL_MSG = "protocol mismatch — update storychat-hermes"
REDIRECT_MSG = "unexpected redirect — check STORYCHAT_URL"


@dataclass(frozen=True)
class Verdict:
    retry: bool
    backoff_start: float
    code: str  # fatal-error code handed to BasePlatformAdapter._set_fatal_error
    message: str


_FATAL_CLOSES = {
    4001: Verdict(False, 0.0, "storychat_replaced", REPLACED_MSG),
    4003: Verdict(False, 0.0, "storychat_token_rejected", TOKEN_MSG),
    4400: Verdict(False, 0.0, "storychat_protocol_mismatch", PROTOCOL_MSG),
    1009: Verdict(False, 0.0, "storychat_protocol_mismatch", PROTOCOL_MSG),
}
_RATE_LIMITED = Verdict(True, BACKOFF_RATE_LIMITED_S, "storychat_rate_limited",
                        "StoryChat is rate limiting this connection; retrying in 60s or more")


def classify_close(code: Optional[int]) -> Verdict:
    """Verdict for a socket that was open (after welcome) and then closed with ``code``."""
    if code in _FATAL_CLOSES:
        return _FATAL_CLOSES[code]
    if code == 4429:
        return _RATE_LIMITED
    return Verdict(True, BACKOFF_START_S, "storychat_disconnected",
                   f"StoryChat connection closed (code {code}); reconnecting")


def classify_upgrade_status(status: int) -> Verdict:
    """Verdict for an HTTP status returned instead of 101 at the websocket upgrade."""
    if 300 <= status < 400:
        return Verdict(False, 0.0, "storychat_redirect", REDIRECT_MSG)
    if status == 401:
        return Verdict(False, 0.0, "storychat_token_rejected", TOKEN_MSG)
    if status == 429:
        return _RATE_LIMITED
    return Verdict(True, BACKOFF_START_S, "storychat_upgrade_failed",
                   f"StoryChat refused the websocket upgrade (HTTP {status}); retrying")


def backoff_delay(attempt: int, start: float, rng: random.Random) -> float:
    """``start`` doubling per attempt, capped at 60s, plus up to 25% jitter (never below the base)."""
    base = min(BACKOFF_MAX_S, start * (2 ** attempt))
    return base + rng.uniform(0, base / 4)


class NoRedirectConnect(_ws_connect):
    """websockets follows 3xx and would re-send the Bearer header to the new host; never do that.
    Returning the exception surfaces the 3xx as ``InvalidStatus`` (spec §9.1)."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc
