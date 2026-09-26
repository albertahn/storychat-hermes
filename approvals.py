"""Approval bookkeeping and the approval PIN (design spec §6 "Adapter approval bookkeeping", §9.3 PIN).

The adapter keeps an ordered list of shown approvals per Hermes ``session_key`` (which never leaves
this process) and only acts on the head. ``PIN_GUARD`` is process-wide so a lockout survives the
adapter being rebuilt by Hermes' reconnect watcher and clears only when the gateway restarts.
"""

from __future__ import annotations

import hmac
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

MAX_PIN_MISSES = 5
MAX_RESOLVED_APPROVALS = 64


class PinGuard:
    """Counts consecutive wrong PINs; 5 in a row locks every remote approval until restart."""

    def __init__(self) -> None:
        self._misses = 0
        self._locked = False

    def check(self, configured_pin: str, supplied: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
        """``(None, None)`` when the PIN is accepted, else ``(ack reason, attemptsLeft or None)``."""
        if not configured_pin:
            return "pin_not_configured", None
        if self._locked:
            return "locked", None
        if supplied is not None and hmac.compare_digest(
                supplied.encode("utf-8"), configured_pin.encode("utf-8")):
            self._misses = 0
            return None, None
        self._misses += 1
        if self._misses >= MAX_PIN_MISSES:
            self._locked = True
            return "locked", 0
        return "bad_pin", MAX_PIN_MISSES - self._misses


PIN_GUARD = PinGuard()


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    session_key: str
    chat_id: str
    turn_id: str
    request_id: str  # Hermes' own id; a card is only booked once it is correlated
    choices: Tuple[str, ...]


class ApprovalBook:
    """Ordered approvals per session_key, oldest first."""

    def __init__(self) -> None:
        self._by_session: Dict[str, List[PendingApproval]] = {}
        # approvalId -> the choice Hermes applied, newest last. Not pending state, so clear()
        # keeps it: a repeated decision is answered with what already happened.
        self._resolved: "OrderedDict[str, str]" = OrderedDict()

    def add(self, entry: PendingApproval) -> None:
        self._by_session[entry.session_key] = [*self._by_session.get(entry.session_key, []), entry]

    def find(self, approval_id: str) -> Optional[PendingApproval]:
        for entries in self._by_session.values():
            for entry in entries:
                if entry.approval_id == approval_id:
                    return entry
        return None

    def head(self, session_key: str) -> Optional[PendingApproval]:
        entries = self._by_session.get(session_key) or []
        return entries[0] if entries else None

    def tracked_request_ids(self, session_key: str) -> Set[str]:
        return {e.request_id for e in self._by_session.get(session_key, []) if e.request_id}

    def _keep(self, session_key: str, keep) -> None:
        kept = [e for e in self._by_session.get(session_key, []) if keep(e)]
        if kept:
            self._by_session[session_key] = kept
        else:
            self._by_session.pop(session_key, None)

    def remove(self, approval_id: str) -> Optional[PendingApproval]:
        entry = self.find(approval_id)
        if entry is not None:
            self._keep(entry.session_key, lambda e: e.approval_id != approval_id)
        return entry

    def reconcile(self, session_key: str, live_request_ids: Set[str]) -> None:
        """Drop entries Hermes no longer holds."""
        self._keep(session_key, lambda e: e.request_id in live_request_ids)

    def drop_chat(self, chat_id: str) -> None:
        for session_key in list(self._by_session):
            self._keep(session_key, lambda e: e.chat_id != chat_id)

    def clear(self) -> None:
        self._by_session = {}

    def mark_resolved(self, approval_id: str, choice: str) -> None:
        self._resolved[approval_id] = choice
        self._resolved.move_to_end(approval_id)
        while len(self._resolved) > MAX_RESOLVED_APPROVALS:
            self._resolved.popitem(last=False)

    def resolved_choice(self, approval_id: str) -> Optional[str]:
        return self._resolved.get(approval_id)


def correlate_request_id(command: str, description: str, live: Iterable[dict],
                         tracked: Set[str]) -> Optional[str]:
    """Find the Hermes queue entry an ExecApprovalPrompt was built from (the prompt carries no id).

    The runner builds ``prompt.command`` as ``_redact_approval_command(entry["command"])`` and
    ``prompt.description`` as ``entry.get("description", "dangerous command")``, so applying the same
    function to each untracked entry matches exactly. Hermes allows several pending approvals per
    session at once (parallel subagents / execute_code), so an untracked entry that does not match
    is some OTHER pending approval, never this card's own — returns ``None`` rather than guessing,
    so the card is refused instead of being bound to an unrelated command.
    """
    from gateway.run import _redact_approval_command
    untracked = [e for e in live if e.get("request_id") and e["request_id"] not in tracked]
    for entry in untracked:
        if (entry.get("description", "dangerous command") == description
                and _redact_approval_command(entry.get("command")) == command):
            return entry["request_id"]
    return None
