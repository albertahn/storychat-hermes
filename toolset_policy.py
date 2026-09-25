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


class ToolsetPolicyError(ValueError):
    """STORYCHAT_TOOLSETS names something Hermes doesn't recognize as a toolset key and that isn't
    a currently enabled MCP server; silently accepting it would leave the intended override
    unclear (and could fall through to the no-allowlist branch that exposes every MCP server)."""


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


def _known_toolset_keys() -> frozenset:
    """Configurable + plugin + platform-default toolset keys — the same three sets
    ``hermes_cli.tools_config._get_platform_tools`` excludes when it computes ``explicit_passthrough``.
    A name outside this union is either an MCP server name or genuinely unknown."""
    from hermes_cli.tools_config import _configurable_keys, _get_plugin_toolset_keys, _platform_default_keys
    return frozenset(_configurable_keys() | _get_plugin_toolset_keys() | _platform_default_keys())


def toolset_override(raw: str, config: Optional[dict] = None) -> List[str]:
    """Chat-only → ``["no_mcp"]``; opt-in → the named toolsets minus clarify, plus ``no_mcp``
    unless the user named an MCP server. "Named an MCP server" follows Hermes's own passthrough
    rule: only names that are NOT a configurable, plugin, or platform-default toolset key count as
    an MCP allowlist entry — otherwise a toolset that happens to share its name with a configured
    MCP server (e.g. a built-in ``memory`` toolset alongside an MCP server also named ``memory``)
    would be mistaken for naming that server, skip the sentinel, and leak every enabled MCP server.

    Raises :class:`ToolsetPolicyError` for a name that resolves to no known toolset and no enabled
    MCP server, instead of silently letting it become an unchecked (and leaky) opt-in."""
    names = [n for n in parse_toolsets(raw) if n not in ALWAYS_REMOVED]
    if not names:
        return list(CHAT_ONLY_SENTINEL)
    from hermes_cli.tools_config import enabled_mcp_server_names
    cfg = load_gateway_config() if config is None else config
    mcp_servers = enabled_mcp_server_names(cfg)
    known_keys = _known_toolset_keys()
    unknown = [n for n in names if n not in CHAT_ONLY_SENTINEL and n not in known_keys and n not in mcp_servers]
    if unknown:
        raise ToolsetPolicyError(
            "STORYCHAT_TOOLSETS names unknown toolset(s): {}; leave STORYCHAT_TOOLSETS empty for "
            "chat only".format(", ".join(unknown)))
    named_mcp_servers = (set(names) - known_keys) & mcp_servers
    if "no_mcp" not in names and not named_mcp_servers:
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
