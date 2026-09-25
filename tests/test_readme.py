"""The setup guide must carry the design spec's §9.2 blocks byte for byte."""

import io
from pathlib import Path

from support import TOKEN, USER_ID

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


def test_troubleshooting_carries_the_clarify_and_display_refusal_messages():
    # Pins README prose added after the plan (task 18 controller rulings 4-5): progress/display
    # settings must stay off, and the clarify + display refusals' fixed leading text (copied from
    # adapter.py's _check_toolsets, which defines no separate message constants for these two)
    # must appear verbatim so the guide cannot silently drift from the code that raises them.
    assert "thinking_progress" in README
    clarify_prefix = (
        "STORYCHAT_TOOLSETS brings in the clarify toolset (through a bundle such as "
        "hermes-cli, coding or all), and StoryChat cannot answer clarify questions, so "
        "turns would hang.")
    assert clarify_prefix in README
    display_prefix = "these StoryChat display settings are still on:"
    assert display_prefix in README
