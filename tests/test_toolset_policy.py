import pytest

from storychat_hermes import toolset_policy
from support import register_storychat_platform

HOST_ACCESS = {"terminal", "file", "code_execution", "browser", "computer_use", "delegation"}
MCP_CONFIG = {"mcp_servers": {"github": {"command": "npx", "args": ["github-mcp"]}}}
# "memory" is a real Hermes configurable toolset AND, here, an MCP server name someone configured
# — the collision Hermes's own passthrough rule (not "does this name match any enabled MCP
# server") is designed to survive.
MULTI_MCP_CONFIG = {"mcp_servers": {
    "memory": {"command": "npx", "args": ["memory-mcp"]},
    "filesystem": {"command": "npx", "args": ["fs-mcp"]},
    "github": {"command": "npx", "args": ["github-mcp"]},
}}


@pytest.fixture(autouse=True)
def _registered():
    register_storychat_platform()


@pytest.mark.parametrize("raw", ["", "   ", "clarify", " , clarify ,"])
def test_chat_only_returns_the_sentinel_never_an_empty_list(raw):
    assert toolset_policy.toolset_override(raw, config={}) == ["no_mcp"]


def test_opt_in_removes_clarify_and_adds_no_mcp():
    assert toolset_policy.toolset_override("web, file terminal,clarify,web", config={}) == [
        "web", "file", "terminal", "no_mcp"]


def test_opt_in_naming_an_mcp_server_keeps_it():
    assert toolset_policy.toolset_override("web,github", config=MCP_CONFIG) == ["web", "github"]


def test_without_an_override_hermes_would_expose_host_tools():
    # Why the sentinel exists: no override falls back to the full hermes-storychat bundle.
    from hermes_cli.tools_config import _get_platform_tools
    assert {"terminal", "file", "code_execution"} <= _get_platform_tools({}, "storychat")


def test_chat_only_guarantee_with_mcp_servers_and_a_context_engine():
    config = {**MCP_CONFIG, "context": {"engine": "lcm"},
              "platform_toolsets": {"storychat": ["terminal", "github"]}}
    effective = toolset_policy.effective_toolsets(["no_mcp"], config=config)
    assert set(effective) <= toolset_policy.CHAT_ONLY_TOLERATED
    assert not set(effective) & HOST_ACCESS
    assert "github" not in effective
    assert toolset_policy.leaked_toolsets(effective) == []


def test_opt_in_resolves_only_the_named_toolsets():
    effective = toolset_policy.effective_toolsets(["terminal", "no_mcp"], config=MCP_CONFIG)
    assert effective == ["terminal"]


def test_leaked_toolsets_ignores_only_x_search_and_context_engine():
    assert toolset_policy.leaked_toolsets(["context_engine", "spotify", "x_search"]) == ["spotify"]


def test_a_toolset_name_that_collides_with_an_mcp_server_still_gets_no_mcp():
    # "memory" is a configurable toolset key, not a passthrough name — naming it must NOT count as
    # "the user named an MCP server", even though an MCP server happens to share the name.
    assert toolset_policy.toolset_override("web,memory", config=MULTI_MCP_CONFIG) == [
        "web", "memory", "no_mcp"]


def test_chat_only_guarantee_survives_an_mcp_server_name_collision():
    # Real-resolver check (spec §14): the "memory" toolset must not smuggle in the other MCP
    # servers (filesystem, github) that happen to share the platform's MCP config.
    override = toolset_policy.toolset_override("web,memory", config=MULTI_MCP_CONFIG)
    effective = toolset_policy.effective_toolsets(override, config=MULTI_MCP_CONFIG)
    assert "filesystem" not in effective
    assert "github" not in effective


def test_naming_a_genuine_mcp_only_server_resolves_only_that_server():
    # "github" is not a configurable/plugin/platform-default key — it IS a real passthrough name,
    # so naming it is a genuine, narrow MCP allowlist entry.
    override = toolset_policy.toolset_override("github", config=MULTI_MCP_CONFIG)
    assert override == ["github"]
    effective = toolset_policy.effective_toolsets(override, config=MULTI_MCP_CONFIG)
    assert effective == ["github"]


def test_rejects_an_unknown_toolset_name_instead_of_silently_ignoring_it():
    with pytest.raises(toolset_policy.ToolsetPolicyError, match="none"):
        toolset_policy.toolset_override("none", config={})
