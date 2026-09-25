import pytest

from storychat_hermes import toolset_policy
from support import register_storychat_platform

HOST_ACCESS = {"terminal", "file", "code_execution", "browser", "computer_use", "delegation"}
MCP_CONFIG = {"mcp_servers": {"github": {"command": "npx", "args": ["github-mcp"]}}}


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
