"""StoryChat relay wire protocol v1 (design spec §6): frame builders, inbound validation, limits.

Every frame is one JSON text message carrying ``v: 1`` and a ``type``. Builders produce the exact
H→S shapes; ``parse_server_frame`` validates the four S→H frames and raises ``ProtocolError``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024  # 1 MiB, equal to the server's maxPayload (spec §6 limits)

APPROVAL_CHOICES = ("once", "session", "always", "deny")
SEND_KINDS = ("reply", "status")
TURN_END_REASONS = ("done", "interrupted", "error")
ACK_REASONS = ("bad_pin", "locked", "pin_not_configured", "not_oldest", "expired")

# turnId / connId / approvalId / msgId: 32-hex or uuid. chatId / userId / messageId: Mongo ObjectIds.
# Case-insensitive, like the relay's own checks.
_OPAQUE_ID_RE = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE)
_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)


class ProtocolError(ValueError):
    """An inbound frame does not match the v1 schema."""


class FrameTooLarge(ValueError):
    """An outbound frame would exceed MAX_FRAME_BYTES (the server would close with 1009)."""


def is_opaque_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_OPAQUE_ID_RE.match(value))


def is_object_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_OBJECT_ID_RE.match(value))


def _encode(frame_type: str, fields: Dict[str, Any]) -> str:
    data = json.dumps({"v": PROTOCOL_VERSION, "type": frame_type, **fields},
                      ensure_ascii=False, separators=(",", ":"))
    if len(data.encode("utf-8")) > MAX_FRAME_BYTES:
        raise FrameTooLarge(f"{frame_type} frame exceeds {MAX_FRAME_BYTES} bytes")
    return data


# ── H→S builders ────────────────────────────────────────────────────────────


def hello(plugin_version: str, effective_toolsets: Sequence[str]) -> str:
    return _encode("hello", {"pluginVersion": plugin_version,
                             "effectiveToolsets": list(effective_toolsets)})


def send(turn_id: str, chat_id: str, msg_id: str, content: str,
         reply_to: Optional[str], kind: str) -> str:
    return _encode("send", {"turnId": turn_id, "chatId": chat_id, "msgId": msg_id,
                            "content": content, "replyTo": reply_to, "kind": kind})


def edit(turn_id: str, chat_id: str, msg_id: str, content: str, final: bool, kind: str) -> str:
    return _encode("edit", {"turnId": turn_id, "chatId": chat_id, "msgId": msg_id,
                            "content": content, "final": final, "kind": kind})


def approval_request(turn_id: str, chat_id: str, approval_id: str, command: str,
                     description: str, choices: Sequence[str], smart_denied: bool,
                     expires_at: int) -> str:
    return _encode("approval_request", {
        "turnId": turn_id, "chatId": chat_id, "approvalId": approval_id, "command": command,
        "description": description, "choices": list(choices), "smartDenied": smart_denied,
        "expiresAt": expires_at})


def approval_ack(approval_id: str, resolved: bool, reason: Optional[str] = None,
                 attempts_left: Optional[int] = None) -> str:
    fields: Dict[str, Any] = {"approvalId": approval_id, "resolved": resolved}
    if reason is not None:
        fields["reason"] = reason
    if attempts_left is not None:
        fields["attemptsLeft"] = attempts_left
    return _encode("approval_ack", fields)


def approval_expired(approval_id: str) -> str:
    return _encode("approval_expired", {"approvalId": approval_id})


def turn_end(turn_id: str, chat_id: str, reason: str) -> str:
    return _encode("turn_end", {"turnId": turn_id, "chatId": chat_id, "reason": reason})


# ── S→H validation ──────────────────────────────────────────────────────────


def _require(frame: Dict[str, Any], key: str, check, what: str) -> Any:
    value = frame.get(key)
    if not check(value):
        raise ProtocolError(f"{frame.get('type')}.{key} must be {what}")
    return value


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


_SERVER_FIELDS: Dict[str, List[tuple]] = {
    "welcome": [("connId", is_opaque_id, "a 32-hex or uuid id"),
                ("userId", is_object_id, "a 24-hex ObjectId")],
    "message": [("turnId", is_opaque_id, "a 32-hex or uuid id"),
                ("chatId", is_object_id, "a 24-hex ObjectId"),
                ("messageId", is_object_id, "a 24-hex ObjectId"),
                ("userId", is_object_id, "a 24-hex ObjectId"),
                ("userName", _is_str, "a string"),
                ("chatName", _is_str, "a string"),
                ("text", _is_nonempty_str, "a non-empty string"),
                ("channelPrompt", _is_str, "a string")],
    "approval_decision": [("approvalId", is_opaque_id, "a 32-hex or uuid id"),
                          ("choice", lambda v: v in APPROVAL_CHOICES, "one of once/session/always/deny")],
    "stop": [("turnId", is_opaque_id, "a 32-hex or uuid id"),
             ("chatId", is_object_id, "a 24-hex ObjectId")],
}


def parse_server_frame(raw: Any) -> Dict[str, Any]:
    """Decode one S→H text frame and validate it against the v1 schema."""
    if not isinstance(raw, str):
        raise ProtocolError("binary frames are not part of protocol v1")
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"frame is not JSON: {exc.msg}") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("frame must be a JSON object")
    if frame.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {frame.get('v')!r}")
    fields = _SERVER_FIELDS.get(frame.get("type"))
    if fields is None:
        raise ProtocolError(f"unknown frame type {frame.get('type')!r}")
    for key, check, what in fields:
        _require(frame, key, check, what)
    if frame["type"] == "approval_decision" and frame.get("pin") is not None and not _is_str(frame["pin"]):
        raise ProtocolError("approval_decision.pin must be a string")
    return frame
