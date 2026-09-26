"""The setup guide must carry the design spec's §9.2 blocks byte for byte."""

import ast
import inspect
import io
import logging
import textwrap
from pathlib import Path

import pytest

from support import TOKEN, USER_ID, make_adapter

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

ENV_BLOCK = """\
# Required. From /chat/hermes (pre-filled there).
STORYCHAT_HERMES_TOKEN=sch_…
# Pre-filled on /chat/hermes.
STORYCHAT_ALLOWED_USERS=<storychat userId>
# Fill in yourself, 6+ digits. Needed to approve commands from StoryChat.
STORYCHAT_APPROVAL_PIN=
# Empty = chat only (default). To opt in, e.g. STORYCHAT_TOOLSETS=web,file,terminal
# MCP servers stay off unless you name them here.
STORYCHAT_TOOLSETS=
# STORYCHAT_URL defaults to wss://prod-rail.storychat.app/api/v1/hermes/connect
# (On non-prod builds /chat/hermes writes STORYCHAT_URL=… here, plus
#  STORYCHAT_DEV_INSECURE_LOCALHOST=1 when the URL is ws://localhost.)
"""

CONFIG_BLOCK = """\
streaming:
  enabled: true              # Hermes default is off; without it replies arrive whole at turn end.
                             # This is global: to keep another platform unchanged, add
                             # display.platforms.<that platform>.streaming: false
display:
  platforms:
    storychat:
      tool_progress: off
      long_running_notifications: off
      interim_assistant_messages: false
"""


def test_env_block_is_exact():
    assert f"```\n{ENV_BLOCK}```" in README


def test_config_block_is_exact():
    assert f"```yaml\n{CONFIG_BLOCK}```" in README


def test_mentions_the_session_age_setting():
    assert "session_store_max_age_days" in README


def test_troubleshooting_carries_the_allowlist_log_line():
    from storychat_hermes import adapter
    assert adapter.NOT_ALLOWED_MSG.format(user_id="<id>") in README


def test_the_pin_is_described_as_passing_through_storychat():
    # The PIN is typed into StoryChat, sent in POST /approvals and relayed in approval_decision:
    # it is stored only in .env, but it does leave the machine.
    text = " ".join(README.split())
    assert "PIN never leave" not in text
    assert "passes through StoryChat's servers to your agent" in text
    assert "use a pin you use nowhere else" in text.lower()


def load_as_hermes_does(monkeypatch, env_text):
    # Hermes loads ~/.hermes/.env with python-dotenv's parser (hermes_cli/env_loader.py
    # _load_dotenv_with_fallback), which reads `KEY=    # note` as the value "# note".
    from dotenv import dotenv_values

    from storychat_hermes import settings
    filled = env_text.replace("sch_…", TOKEN).replace("<storychat userId>", USER_ID)
    for name, value in dotenv_values(stream=io.StringIO(filled), interpolate=False).items():
        monkeypatch.setenv(name, value)
    return settings.load_settings()


def test_the_env_block_as_hermes_parses_it_is_chat_only_without_a_pin(monkeypatch):
    from storychat_hermes import settings, toolset_policy
    cfg = load_as_hermes_does(monkeypatch, ENV_BLOCK)
    assert (cfg.token, cfg.allowed_users, cfg.approval_pin, cfg.toolsets_raw) == (
        TOKEN, (USER_ID,), "", "")
    # The STORYCHAT_URL lines are comments: the prod relay, and no ws:// allowance.
    assert (cfg.url, cfg.is_local_dev) == (settings.DEFAULT_URL, False)
    assert toolset_policy.toolset_override(cfg.toolsets_raw, config={}) == ["no_mcp"]


def test_a_non_prod_block_with_both_url_lines_reaches_the_local_relay(monkeypatch):
    # The two lines /chat/hermes writes on a non-prod build pointed at a local backend (spec §9.2).
    local = ENV_BLOCK + ("STORYCHAT_URL=ws://localhost:8080/api/v1/hermes/connect\n"
                         "STORYCHAT_DEV_INSECURE_LOCALHOST=1\n")
    cfg = load_as_hermes_does(monkeypatch, local)
    assert (cfg.url, cfg.is_local_dev) == ("ws://localhost:8080/api/v1/hermes/connect", True)


def _logger_error_format(func, keyword: str, level: str = "error") -> str:
    """Return the literal format string passed to a ``logger.<level>(...)`` call inside ``func``
    whose text contains ``keyword``. Reads the AST rather than re-typing the message, so adjacent
    string literals are folded exactly as Python folds them at parse time and the check breaks the
    moment the source message changes, instead of silently drifting from a copied literal."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == level and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                and keyword in node.args[0].value):
            return node.args[0].value
    raise AssertionError(f"no logger.{level}(...) call containing {keyword!r} found in {func!r}")


def test_troubleshooting_carries_the_refusals_added_after_the_plan():
    # Pins every troubleshooting row added after the plan (task 18 controller rulings 3-5): the
    # unknown-toolset, clarify and display refusals, the per-turn refusal, and the "progress
    # messages stay off" prose. Each fixed string is derived from the module that raises it —
    # either a real raised exception or the AST of the logger.error(...) call — rather than
    # copied as a literal, so a change to the source message fails this test instead of letting
    # the README silently drift from the code.
    from storychat_hermes import adapter, toolset_policy

    assert "thinking_progress" in README

    clarify_prefix = (
        "STORYCHAT_TOOLSETS brings in the clarify toolset (through a bundle such as "
        "hermes-cli, coding or all), and StoryChat cannot answer clarify questions, so "
        "turns would hang.")
    assert clarify_prefix in README

    display_prefix = "these StoryChat display settings are still on:"
    assert display_prefix in README

    # Unknown-toolset refusal (toolset_policy.ToolsetPolicyError): trigger the real exception and
    # split its message around the unknown name to get the fixed prefix/suffix around it.
    try:
        toolset_policy.toolset_override("definitely-not-a-real-toolset", config={})
        raise AssertionError("expected ToolsetPolicyError")
    except toolset_policy.ToolsetPolicyError as exc:
        unknown_prefix, _, unknown_suffix = str(exc).partition("definitely-not-a-real-toolset")
    assert unknown_prefix and unknown_prefix in README
    assert unknown_suffix and unknown_suffix in README

    # Per-turn refusal (adapter.py _on_message logs it, there is no message constant): pull the
    # literal logger.error format string and take the fixed part after "refused turn %s".
    per_turn_fmt = _logger_error_format(adapter.StoryChatAdapter._on_message, "refused turn")
    _, _, per_turn_suffix = per_turn_fmt.partition("refused turn %s")
    assert per_turn_suffix
    for reason in (adapter.TOOLSETS_CHANGED, adapter.DISPLAY_CHANGED, adapter.RECHECK_UNVERIFIED):
        assert f"refused turn …{per_turn_suffix % reason}" in README


def test_the_reconnect_delay_matches_the_backoff():
    from storychat_hermes import lifecycle

    class Edge:
        def __init__(self, pick):
            self.pick = pick

        def uniform(self, low, high):
            return self.pick(low, high)

    fastest = lifecycle.backoff_delay(0, lifecycle.BACKOFF_START_S, Edge(min))
    slowest = lifecycle.backoff_delay(99, lifecycle.BACKOFF_START_S, Edge(max))
    text = " ".join(README.split())
    assert f"{fastest:g}–{slowest:g} seconds" in text
    assert f"after {lifecycle.MAX_RECONNECT_ATTEMPTS} failed attempts Hermes' own watcher takes over" in text


def test_the_no_mcp_warning_is_quoted_in_full(monkeypatch, caplog):
    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_warned_invalid_platform_toolsets", set())
    caplog.set_level(logging.WARNING, logger=tools_config.logger.name)
    tools_config._warn_all_invalid_platform_toolsets("storychat", ["no_mcp"])
    (line,) = [r.getMessage() for r in caplog.records if "no_mcp" in r.getMessage()]
    assert line in README
    assert "the empty set is intended" in README


@pytest.mark.asyncio
async def test_troubleshooting_covers_the_token_lock_and_rate_limiting(monkeypatch, storychat_env):
    import gateway.status

    from storychat_hermes import lifecycle
    monkeypatch.setattr(gateway.status, "acquire_scoped_lock",
                        lambda scope, identity, metadata=None: (False, {"pid": 4242}))
    adapter = make_adapter()
    assert await adapter.connect() is False
    held, in_use, _ = adapter.fatal_error_message.partition(" already in use")
    assert in_use and f"{held}{in_use}" in README
    assert lifecycle._RATE_LIMITED.message in README


@pytest.mark.asyncio
async def test_troubleshooting_covers_the_toolset_limits_of_hello(monkeypatch, storychat_env):
    import hermes_cli.tools_config
    monkeypatch.setattr(hermes_cli.tools_config, "_get_platform_tools",
                        lambda config, platform, **kw: {"m" * 129})
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "web")
    adapter = make_adapter()
    assert await adapter.connect() is False
    prefix, resolves, _ = adapter.fatal_error_message.partition(" resolves to ")
    assert resolves and f"| `{prefix}{resolves}…` |" in README


def test_troubleshooting_covers_proxy_errors_and_busy_turns():
    from storychat_hermes import adapter
    assert f"| `{adapter.PROXY_MSG}` |" in README
    busy_fmt = _logger_error_format(adapter.StoryChatAdapter._on_message, "still finishing",
                                    level="warning")
    _, _, busy_suffix = busy_fmt.partition("refused turn %s")
    assert busy_suffix and f"| `refused turn …{busy_suffix}` |" in README
    assert "wait a moment and send the message again" in README.lower()
