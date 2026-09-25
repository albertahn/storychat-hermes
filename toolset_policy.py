"""Which Hermes toolsets the storychat platform may use (design spec §9.3, decision D4).

``toolset_override`` is what ``StoryChatAdapter.toolsets_for_source`` returns. It is never ``[]``:
Hermes treats an empty list as "no override" and falls back to the full ``hermes-storychat`` bundle.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

PLATFORM = "storychat"
CHAT_ONLY_SENTINEL = ("no_mcp",)
# The only toolsets chat-only mode tolerates: neither can touch the filesystem or run commands.
CHAT_ONLY_TOLERATED = frozenset({"x_search", "context_engine"})
# A clarify prompt would hang the turn: the relay cannot deliver the typed answer it waits for.
ALWAYS_REMOVED = frozenset({"clarify"})


def load_gateway_config() -> dict:
    """The gateway's effective config.yaml, exactly as the runner loads it for every turn."""
    from gateway.run import _load_gateway_config
    return _load_gateway_config()


def parse_toolsets(raw: str) -> List[str]:
    """Split STORYCHAT_TOOLSETS on commas/whitespace; keep first-seen order, drop duplicates."""
    seen: List[str] = []
    for name in re.split(r"[\s,]+", raw or ""):
        if name and name not in seen:
            seen.append(name)
    return seen


def toolset_override(raw: str, config: Optional[dict] = None) -> List[str]:
    """Chat-only → ``["no_mcp"]``; opt-in → the named toolsets minus clarify, plus ``no_mcp``
    unless the user named an MCP server."""
    names = [n for n in parse_toolsets(raw) if n not in ALWAYS_REMOVED]
    if not names:
        return list(CHAT_ONLY_SENTINEL)
    from hermes_cli.tools_config import enabled_mcp_server_names
    mcp_servers = enabled_mcp_server_names(load_gateway_config() if config is None else config)
    if "no_mcp" not in names and not set(names) & mcp_servers:
        names.append("no_mcp")
    return names


def is_chat_only(override: Iterable[str]) -> bool:
    return list(override) == list(CHAT_ONLY_SENTINEL)


def effective_toolsets(override: List[str], config: Optional[dict] = None) -> List[str]:
    """Resolve the override the same way the runner does: the real config with
    ``platform_toolsets.storychat`` replaced (gateway/run_turn.py ``_resolve_enabled_toolsets_for_source``)."""
    from hermes_cli.tools_config import _get_platform_tools
    cfg = load_gateway_config() if config is None else config
    pts = dict(cfg.get("platform_toolsets") or {})
    pts[PLATFORM] = [str(x) for x in override]
    return sorted(_get_platform_tools({**cfg, "platform_toolsets": pts}, PLATFORM))


def leaked_toolsets(effective: Iterable[str]) -> List[str]:
    """Toolsets that must not be present in chat-only mode."""
    return sorted(set(effective) - CHAT_ONLY_TOLERATED)
