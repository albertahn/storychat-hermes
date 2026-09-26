"""Hermes' own texts that must never be saved as the character's reply (design spec §7, §13).

Hermes sends these through the same plain ``send()`` as a reply, with no marker. The relay saves
every kind "reply" send as the character's message, so the adapter recognises the fixed templates
and sends them as kind "status". Every template lives here, named after the Hermes code that writes
it; tests run Hermes' real helpers where they exist, so a wording change on Hermes main fails CI.
"""

from __future__ import annotations

from typing import Optional

FAILURE = "failure"  # the turn failed or never ran: status, and the turn ends with "error"

# Hermes ends these turns as SUCCESS (its error text was delivered), so the relay would otherwise
# get turn_end "done" and keep the error text as the reply.
_FAILURE_PREFIXES = (
    # gateway/run.py _normalize_empty_agent_response (a failed turn) and gateway/run_turn.py
    # _hmwa_agent_error_reply (the agent raised)
    "⚠️ Something went wrong and I couldn't finish this reply",
    # _normalize_empty_agent_response: interrupted before any model call (a stale /stop)
    "⚠️ Your message was interrupted before processing started",
    # _normalize_empty_agent_response: the previous turn was still being cleaned up
    "⚠️ Your message wasn't processed",
    # _normalize_empty_agent_response: model calls but no text
    "⚠️ Processing completed but no response was generated",
    # _normalize_empty_agent_response: a partial turn with no text
    "⚠️ I had to stop before finishing",
    # gateway/run_turn_runner.py run_sync: the model or its credentials could not be resolved
    "⚠️ I couldn't connect to the AI model service",
    # agent/conversation_loop.py _billing_terminal_label
    "Billing or credits exhausted:",
)


def classify(content: str) -> Optional[str]:
    """``FAILURE`` for Hermes' failed-turn copy, None for text that may be the character's reply."""
    from agent.turn_failure_copy import FAILED_TURN_NOTICE, PARTIAL_FAILED_TURN_NOTICE
    from gateway.run import _CONTEXT_OVERFLOW_REPLY
    text = content.strip()
    if (text.startswith(_FAILURE_PREFIXES + (_CONTEXT_OVERFLOW_REPLY,))
            # gateway/run_turn.py _hmwa_add_failed_turn_notice ends every failed turn's reply so
            or text.endswith((FAILED_TURN_NOTICE, PARTIAL_FAILED_TURN_NOTICE))):
        return FAILURE
    return None
