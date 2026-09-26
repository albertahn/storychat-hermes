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
    Used ONLY to decide whether a name counts as an explicitly named MCP server: a name outside
    this union is a real passthrough entry (an MCP server name, or something Hermes leaves
    unresolved). This is deliberately narrower than "every name Hermes accepts" — see
    ``validate_toolset`` in :func:`toolset_override` for that broader validity check."""
    from hermes_cli.tools_config import _configurable_keys, _get_plugin_toolset_keys, _platform_default_keys
    return frozenset(_configurable_keys() | _get_plugin_toolset_keys() | _platform_default_keys())


def toolset_override(raw: str, config: Optional[dict] = None) -> List[str]:
    """Chat-only → ``["no_mcp"]``, decided before any config I/O; opt-in → the named toolsets minus
    clarify, plus ``no_mcp`` unless the user named an MCP server. "Named an MCP server" follows
    Hermes's own passthrough rule: only names that are NOT a configurable, plugin, or
    platform-default toolset key count as an MCP allowlist entry — otherwise a toolset that happens
    to share its name with a configured MCP server (e.g. a built-in ``memory`` toolset alongside an
    MCP server also named ``memory``) would be mistaken for naming that server, skip the sentinel,
    and leak every enabled MCP server.

    Raises :class:`ToolsetPolicyError` for a name Hermes itself would not accept: valid names are
    whatever ``toolsets.validate_toolset`` recognizes (composites like ``all``/``*``, postures like
    ``debugging``/``safe``/``coding``, and every configurable/plugin toolset) plus any configured
    MCP server name — anything outside that is a typo Hermes could not resolve either, and must
    not silently become an unchecked (and potentially leaky) opt-in."""
    names = [n for n in parse_toolsets(raw) if n not in ALWAYS_REMOVED]
    if not names:
        return list(CHAT_ONLY_SENTINEL)
    from hermes_cli.tools_config import enabled_mcp_server_names
    from toolsets import validate_toolset
    cfg = load_gateway_config() if config is None else config
    mcp_servers = enabled_mcp_server_names(cfg)
    unknown = [n for n in names
               if n not in CHAT_ONLY_SENTINEL and not validate_toolset(n) and n not in mcp_servers]
    if unknown:
        raise ToolsetPolicyError(
            "STORYCHAT_TOOLSETS names unknown toolset(s): {}; leave STORYCHAT_TOOLSETS empty for "
            "chat only".format(", ".join(unknown)))
    named_mcp_servers = (set(names) - _known_toolset_keys()) & mcp_servers
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


def external_memory_provider(config: Optional[dict] = None) -> Optional[str]:
    """The external memory provider Hermes runs on every turn, or None for the built-in store only.
    Whatever the toolsets, the provider prefetches recall into each turn and writes each turn back
    (agent/memory_manager.py prefetch_all / sync_all). Resolved like agent/agent_init.py
    _init_memory: ``memory.provider``, where "", default, builtin, built-in and none mean the
    built-in store (agent/memory_provider.py is_core_memory_provider)."""
    from agent.memory_provider import is_core_memory_provider
    from tools.memory_tool import get_builtin_memory_config
    cfg = load_gateway_config() if config is None else config
    name = get_builtin_memory_config(cfg).get("provider", "")
    return None if is_core_memory_provider(name) else str(name).strip()


def unsafe_display_settings(config: Optional[dict] = None) -> List[str]:
    """Gateway display surfaces that would still stream to StoryChat as the character's reply
    (spec §9.2): unlike ``long_running_notifications`` (always sent with ``_interim_send`` —
    ``gateway/run.py`` ``_interim_metadata``, ``run_turn.py`` L4208), tool-progress and interim-
    assistant lines carry no such marker for storychat (``_non_conversational_metadata`` only
    special-cases Discord), so if they resolve ON they would go out with ``send`` kind "reply" and
    get saved as the character's message. Resolved exactly the way ``gateway/run_turn.py``
    (L2961-3014) resolves them for a live turn."""
    from agent.secret_scope import get_secret
    from gateway.display_config import resolve_display_setting, resolve_tool_progress
    cfg = load_gateway_config() if config is None else config
    unsafe: List[str] = []
    progress_mode, _explicit = resolve_tool_progress(cfg, PLATFORM, get_secret("HERMES_TOOL_PROGRESS_MODE"))
    if progress_mode not in {"off", "log"}:
        unsafe.append("tool_progress")
    for setting, default in (("interim_assistant_messages", True), ("thinking_progress", False)):
        value = resolve_display_setting(cfg, PLATFORM, setting, default)
        is_generic = isinstance(value, str) and value.strip().lower() == "generic"
        if not is_generic and bool(value):
            unsafe.append(setting)
    return sorted(unsafe)
