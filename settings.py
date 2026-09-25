"""STORYCHAT_* configuration (design spec §9.1–§9.3), read through Hermes' scoped-secret reader."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Tuple
from urllib.parse import urlsplit

from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret

logger = logging.getLogger(__name__)

DEFAULT_URL = "wss://prod-rail.storychat.app/api/v1/hermes/connect"
_TOKEN_RE = re.compile(r"^sch_[0-9a-f]{64}$")
_PIN_RE = re.compile(r"^[0-9]{6,}$")
_LOCALHOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


class SettingsError(ValueError):
    """A STORYCHAT_* value is missing or unusable; the adapter must not connect."""


@dataclass(frozen=True)
class StoryChatSettings:
    token: str
    url: str
    approval_pin: str  # "" when unset or not 6+ digits: remote approvals answer pin_not_configured
    toolsets_raw: str
    is_local_dev: bool  # ws://localhost with STORYCHAT_DEV_INSECURE_LOCALHOST=1
    allowed_users: Tuple[str, ...]  # STORYCHAT_ALLOWED_USERS entries, trimmed, spelled as written


def _secret(name: str) -> str:
    value = str(_get_scoped_secret(name, "") or "").strip()
    # Hermes parses ~/.hermes/.env with python-dotenv, which reads `KEY=    # note` as the value
    # "# note". An empty setting followed by a trailing comment, as older setup guides wrote it,
    # must stay empty.
    return "" if value.startswith("#") else value


def validate_url(url: str, *, allow_insecure_localhost: bool) -> bool:
    """Return True for an allowed ws:// localhost dev URL, False for wss://; raise otherwise."""
    parts = urlsplit(url)
    if parts.scheme == "wss" and parts.hostname:
        return False
    if parts.scheme == "ws" and parts.hostname in _LOCALHOST_NAMES and allow_insecure_localhost:
        return True
    if parts.scheme == "ws":
        raise SettingsError(
            "STORYCHAT_URL uses ws://, which is refused unless STORYCHAT_DEV_INSECURE_LOCALHOST=1 "
            "and the host is localhost")
    raise SettingsError("STORYCHAT_URL must be a wss:// URL")


def load_settings() -> StoryChatSettings:
    token = _secret("STORYCHAT_HERMES_TOKEN")
    if not _TOKEN_RE.match(token):
        raise SettingsError(
            "token invalid or revoked — generate a new one at storychat.app/chat/hermes")
    url = _secret("STORYCHAT_URL") or DEFAULT_URL
    is_local_dev = validate_url(
        url, allow_insecure_localhost=_secret("STORYCHAT_DEV_INSECURE_LOCALHOST") == "1")
    pin = _secret("STORYCHAT_APPROVAL_PIN")
    if pin and not _PIN_RE.match(pin):
        logger.warning("STORYCHAT_APPROVAL_PIN must be 6 or more digits; remote approvals stay "
                       "disabled until it is fixed")
        pin = ""
    allowed_users = tuple(u.strip() for u in _secret("STORYCHAT_ALLOWED_USERS").split(",") if u.strip())
    return StoryChatSettings(token=token, url=url, approval_pin=pin,
                             toolsets_raw=_secret("STORYCHAT_TOOLSETS"), is_local_dev=is_local_dev,
                             allowed_users=allowed_users)
