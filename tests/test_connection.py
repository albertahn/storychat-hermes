import asyncio
import logging
import os
from pathlib import Path

import pytest

from support import CHAT_ID, TOKEN, FakeStoryChat, make_adapter, message_frame, wait_until

pytestmark = pytest.mark.asyncio


async def test_hello_welcome_marks_connected(monkeypatch, storychat_env):
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect(is_reconnect=False) is True
        assert adapter.is_connected is True
        assert server.hellos == [{"v": 1, "type": "hello", "pluginVersion": "0.1.0",
                                  "effectiveToolsets": []}]
        headers = server.requests[0].headers
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert "Origin" not in headers
        assert headers["User-Agent"] == "storychat-hermes/0.1.0"
        await adapter.disconnect()
        assert adapter.is_connected is False


async def test_token_lock_is_held_while_connected_and_never_stores_the_token(monkeypatch, storychat_env):
    lock_dir = Path(os.environ["HERMES_GATEWAY_LOCK_DIR"])
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        locks = list(lock_dir.glob("storychat-hermes-token-*.lock"))
        assert len(locks) == 1
        assert TOKEN not in locks[0].read_text()
        await adapter.disconnect()
        assert list(lock_dir.glob("storychat-hermes-token-*.lock")) == []


async def test_token_held_by_another_gateway_refuses_to_connect(monkeypatch, storychat_env):
    import gateway.status
    monkeypatch.setattr(gateway.status, "acquire_scoped_lock",
                        lambda scope, identity, metadata=None: (False, {"pid": 4242}))
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert adapter.fatal_error_code == "storychat-hermes-token_lock"
        assert server.requests == []


async def test_leaked_toolsets_refuse_to_connect(monkeypatch, storychat_env):
    import hermes_cli.tools_config
    monkeypatch.setattr(hermes_cli.tools_config, "_get_platform_tools",
                        lambda config, platform, **kw: {"terminal", "x_search"})
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (
            "storychat_toolsets_leaked", False)
        assert "terminal" in adapter.fatal_error_message
        assert server.requests == []


async def test_unsafe_display_settings_refuse_to_connect(monkeypatch, storychat_env):
    from storychat_hermes import toolset_policy
    monkeypatch.setattr(toolset_policy, "load_gateway_config", lambda: {})
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (
            "storychat_display_unsafe", False)
        assert "tool_progress" in adapter.fatal_error_message
        assert server.requests == []


async def test_opt_in_warns_once_about_character_cards(monkeypatch, storychat_env, caplog):
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "web")
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        for _ in range(2):
            adapter = make_adapter()
            assert await adapter.connect() is True
            await adapter.disconnect()
        assert server.hellos[0]["effectiveToolsets"] == ["web"]
    warnings = [r.getMessage() for r in caplog.records if "character cards" in r.getMessage()]
    assert len(warnings) == 1
    # Hermes skips the card for commands on the permanent allowlist (tools/approval.py
    # check_all_command_guards) and for those smart approvals pass.
    assert "every dangerous command" not in warnings[0]
    assert "command_allowlist" in warnings[0] and "smart" in warnings[0]


def hermes_approvals(monkeypatch, mode=None, yolo=False):
    """approvals.mode as config.yaml's YAML gives it to Hermes (a bare `off` parses as False)."""
    import tools.approval
    import tools.approval_context
    monkeypatch.setattr(tools.approval_context, "_get_approval_config",
                        lambda: {} if mode is None else {"mode": mode})
    monkeypatch.setattr(tools.approval, "_YOLO_MODE_FROZEN", yolo)


@pytest.mark.parametrize("mode,yolo", [(False, False), ("off", False), (None, True)])
async def test_opt_in_refuses_to_connect_when_hermes_approvals_are_off(monkeypatch, storychat_env,
                                                                      mode, yolo):
    # approvals.mode: off or --yolo (HERMES_YOLO_MODE) makes Hermes run every dangerous command
    # without asking, so the StoryChat PIN would guard nothing.
    hermes_approvals(monkeypatch, mode, yolo)
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "terminal")
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (
            "storychat_approvals_off", False)
        assert server.requests == []


@pytest.mark.parametrize("toolsets,mode", [("terminal", "smart"), ("terminal", "manual"), ("", False)])
async def test_smart_approvals_or_chat_only_still_connect(monkeypatch, storychat_env, toolsets, mode):
    hermes_approvals(monkeypatch, mode)
    monkeypatch.setenv("STORYCHAT_TOOLSETS", toolsets)
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        await adapter.disconnect()


@pytest.mark.parametrize("status,code", [(401, "storychat_token_rejected"),
                                         (302, "storychat_redirect")])
async def test_upgrade_refusals_are_fatal(monkeypatch, storychat_env, status, code):
    async with FakeStoryChat() as elsewhere, FakeStoryChat(
            upgrade_status=None if status == 401 else status) as server:
        if status == 302:
            server.location = elsewhere.url
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        if status == 401:
            monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", "sch_" + "cd" * 32)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (code, False)
        assert elsewhere.requests == []  # the Bearer header never followed the redirect


@pytest.mark.parametrize("close_code,code", [(4003, "storychat_token_rejected"),
                                             (4400, "storychat_protocol_mismatch"),
                                             (4429, "storychat_rate_limited")])
async def test_close_during_hello(monkeypatch, storychat_env, close_code, code):
    async with FakeStoryChat(close_on_hello=close_code) as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert adapter.fatal_error_code == code
        assert adapter.fatal_error_retryable is (close_code == 4429)


async def test_unreachable_relay_is_retryable(monkeypatch, storychat_env):
    async with FakeStoryChat() as server:
        url = server.url
    monkeypatch.setenv("STORYCHAT_URL", url)  # nothing listens on this port any more
    adapter = make_adapter()
    assert await adapter.connect() is False
    assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == ("storychat_unreachable", True)


async def test_debug_logging_never_prints_the_token(monkeypatch, storychat_env, caplog):
    caplog.set_level(logging.DEBUG)
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        await adapter.disconnect()
    assert TOKEN not in caplog.text


async def test_nothing_is_sent_without_a_running_turn(monkeypatch, storychat_env):
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        assert (await adapter.send(CHAT_ID, "hello?")).success is False
        assert server.frames.empty()
        await adapter.disconnect()


async def test_toolsets_for_source_is_never_empty():
    adapter = make_adapter()
    assert adapter.toolsets_for_source(None) == ["no_mcp"]
    assert await adapter.get_chat_info(CHAT_ID) == {"name": CHAT_ID, "type": "dm", "chat_id": CHAT_ID}


async def test_unknown_toolset_name_refuses_to_connect(monkeypatch, storychat_env):
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "definitely_not_a_toolset")
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (
            "storychat_toolsets_invalid", False)
        assert "definitely_not_a_toolset" in adapter.fatal_error_message
        assert server.requests == []


@pytest.mark.parametrize("resolved", [{"web", "m" * 129}, {f"mcp{i}" for i in range(101)}])
async def test_toolsets_the_relay_would_reject_in_hello_refuse_to_connect(monkeypatch, storychat_env,
                                                                         resolved):
    # The relay closes with 4400 on a hello listing more than 100 toolsets or a name over 128
    # characters, which the plugin would report as "protocol mismatch — update storychat-hermes".
    import hermes_cli.tools_config
    monkeypatch.setattr(hermes_cli.tools_config, "_get_platform_tools",
                        lambda config, platform, **kw: set(resolved))
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "web")
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (
            "storychat_toolsets_invalid", False)
        assert server.requests == []


async def test_the_most_toolsets_the_relay_accepts_still_connect(monkeypatch, storychat_env):
    import hermes_cli.tools_config
    from storychat_hermes import protocol
    resolved = {f"mcp{i}" for i in range(protocol.MAX_HELLO_TOOLSETS - 1)} | {
        "m" * protocol.MAX_TOOLSET_NAME_CHARS}
    monkeypatch.setattr(hermes_cli.tools_config, "_get_platform_tools",
                        lambda config, platform, **kw: set(resolved))
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "web")
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        assert server.hellos[0]["effectiveToolsets"] == sorted(resolved)
        await adapter.disconnect()


async def test_clarify_toolset_refuses_to_connect(monkeypatch, storychat_env):
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "coding")
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (
            "storychat_toolsets_clarify", False)
        assert server.requests == []


@pytest.mark.parametrize("allowed", ["ffffffffffffffffffffffff", ""])
async def test_welcome_user_missing_from_the_allowlist_refuses_to_connect(monkeypatch, storychat_env,
                                                                          caplog, allowed):
    monkeypatch.setenv("STORYCHAT_ALLOWED_USERS", allowed)
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert len(server.hellos) == 1  # the relay answered with welcome; the refusal is the plugin's
        # Cross-plan contract 9: a normal close right after welcome, before any other frame.
        await asyncio.wait_for(server.connections[-1].wait_closed(), 3)
        assert server.connections[-1].close_code == 1000
        assert server.frames.empty()
        assert list(Path(os.environ["HERMES_GATEWAY_LOCK_DIR"]).glob("storychat-hermes-token-*.lock")) == []
    message = "STORYCHAT_ALLOWED_USERS must contain your StoryChat userId 64b0000000000000000000a1 — copy it from storychat.app/chat/hermes"
    assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == ("storychat_user_not_allowed", False)
    assert adapter.fatal_error_message == message
    assert f"[{adapter.name}] {message}" in caplog.messages
    assert adapter.is_connected is False


async def test_allowlist_entries_are_trimmed_and_matched_ignoring_case(monkeypatch, storychat_env):
    monkeypatch.setenv("STORYCHAT_ALLOWED_USERS", " ffffffffffffffffffffffff ,  64B0000000000000000000A1 ")
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        # Hermes' own allowlist check is an exact string match, so it must get the listed spelling.
        assert adapter._user_id == "64B0000000000000000000A1"
        await adapter.disconnect()


def fatal_recorder(adapter):
    calls = []

    async def handler(failed):
        calls.append((failed.fatal_error_code, failed.fatal_error_retryable))

    adapter.set_fatal_error_handler(handler)
    return calls


@pytest.mark.parametrize("close_code,code", [(4001, "storychat_replaced"),
                                             (4003, "storychat_token_rejected"),
                                             (4400, "storychat_protocol_mismatch"),
                                             (1009, "storychat_protocol_mismatch")])
async def test_fatal_close_codes_stop_retrying(monkeypatch, storychat_env, close_code, code):
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        fatal = fatal_recorder(adapter)
        assert await adapter.connect() is True
        await server.close_client(close_code)
        await wait_until(lambda: fatal)
        assert fatal == [(code, False)]
        assert len(server.hellos) == 1
        await adapter.disconnect()


async def test_unexpected_close_reconnects_on_the_same_instance(monkeypatch, storychat_env):
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        await server.close_client(1011)
        await wait_until(lambda: len(server.hellos) == 2 and adapter.is_connected)
        assert 1.0 <= adapter.slept[0] <= 1.25
        await adapter.disconnect()


async def test_rate_limited_close_backs_off_from_sixty_seconds(monkeypatch, storychat_env):
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        await server.close_client(4429)
        await wait_until(lambda: len(server.hellos) == 2)
        assert 60.0 <= adapter.slept[0] <= 75.0
        await adapter.disconnect()


async def test_hands_off_to_hermes_after_ten_failed_reconnects(monkeypatch, storychat_env):
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        fatal = fatal_recorder(adapter)
        gate = asyncio.Event()

        async def gated_sleep(delay):
            adapter.slept.append(delay)
            await gate.wait()

        adapter._sleep = gated_sleep
        assert await adapter.connect() is True
        await server.close_client(1012)
        await wait_until(lambda: adapter.slept)  # the adapter is waiting to reconnect
        server._server.close()
        await server._server.wait_closed()  # from now on every reconnect is refused
        gate.set()
        await wait_until(lambda: fatal)
        assert fatal == [("storychat_unreachable", True)]
        assert len(adapter.slept) == 10
        await adapter.disconnect()


async def test_disconnect_during_reconnect_closes_the_half_open_socket(monkeypatch, storychat_env):
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        server.silent_after_hello = True
        await server.close_client(1011)
        await wait_until(lambda: len(server.hellos) == 2)
        await adapter.disconnect()
        await asyncio.wait_for(server.connections[-1].wait_closed(), 3)


async def test_reconnect_stops_turns_hermes_is_still_running(monkeypatch, storychat_env):
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        await server.push("message", **message_frame())
        await wait_until(lambda: adapter.handle_message.await_count == 1)
        turn_event = adapter.handle_message.await_args.args[0]
        adapter._active_sessions[adapter._event_session_key(turn_event)] = object()
        await server.close_client(1012)
        await wait_until(lambda: adapter.handle_message.await_count == 2)
        stop_event = adapter.handle_message.await_args.args[0]
        assert (stop_event.text, stop_event.allow_gateway_control) == ("/stop", True)
        assert stop_event.source.chat_id == turn_event.source.chat_id
        await adapter.disconnect()


PROXY_ENV = ("https_proxy", "HTTPS_PROXY", "wss_proxy", "WSS_PROXY", "socks_proxy", "SOCKS_PROXY",
             "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY")
REMOTE_URL = "wss://relay.storychat.invalid/api/v1/hermes/connect"


def storychat_locks():
    return list(Path(os.environ["HERMES_GATEWAY_LOCK_DIR"]).glob("storychat-hermes-token-*.lock"))


@pytest.mark.parametrize("proxy", ["ftp://127.0.0.1:9", "socks5://127.0.0.1:9"])
async def test_unusable_proxy_settings_are_fatal_and_release_the_lock(monkeypatch, storychat_env, proxy):
    # websockets raises InvalidProxy for an unsupported proxy URL and ImportError for a SOCKS proxy
    # without python-socks; neither may escape connect() still holding the token lock.
    import websockets.asyncio.client as ws_client

    async def no_python_socks(*args, **kwargs):
        raise ImportError("python-socks is required to use a SOCKS proxy")

    # Patched so the SOCKS case behaves the same where python-socks happens to be installed.
    monkeypatch.setattr(ws_client, "connect_socks_proxy", no_python_socks)
    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("STORYCHAT_URL", REMOTE_URL)
    adapter = make_adapter()
    assert await adapter.connect() is False
    assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == ("storychat_proxy_invalid", False)
    assert adapter.fatal_error_message == (
        "proxy settings are invalid or need python-socks — check HTTPS_PROXY/WSS_PROXY/SOCKS_PROXY")
    assert storychat_locks() == []


async def test_an_unexpected_connect_error_is_retryable_and_releases_the_lock(monkeypatch, storychat_env):
    from storychat_hermes import lifecycle

    async def broken(*args, **kwargs):
        raise RuntimeError("unexpected detail")

    monkeypatch.setattr(lifecycle, "NoRedirectConnect", broken)
    monkeypatch.setenv("STORYCHAT_URL", REMOTE_URL)
    adapter = make_adapter()
    assert await adapter.connect() is False
    assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == ("storychat_unreachable", True)
    assert "RuntimeError" in adapter.fatal_error_message
    assert "unexpected detail" not in adapter.fatal_error_message
    assert storychat_locks() == []


async def test_a_crashed_connection_task_is_handed_to_hermes(monkeypatch, storychat_env, caplog):
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        fatal = fatal_recorder(adapter)

        async def crash():
            raise RuntimeError("unexpected detail")

        adapter._pump = crash
        assert await adapter.connect() is True
        await wait_until(lambda: fatal)
        assert fatal == [("storychat_connection_failed", True)]
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("RuntimeError" in m for m in errors)
        assert "unexpected detail" not in caplog.text
        await adapter.disconnect()


async def test_a_toolset_check_that_cannot_run_keeps_its_detail_at_debug(monkeypatch, storychat_env,
                                                                        caplog):
    caplog.set_level(logging.DEBUG)
    from storychat_hermes import toolset_policy

    def broken(override, config=None):
        raise RuntimeError("unexpected detail")

    monkeypatch.setattr(toolset_policy, "effective_toolsets", broken)
    adapter = make_adapter()
    assert await adapter.connect() is False
    assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (
        "storychat_toolsets_unverified", False)
    assert any(r.levelno == logging.DEBUG and r.exc_info for r in caplog.records
               if r.name == "storychat_hermes.adapter")


async def test_a_display_check_that_cannot_run_refuses_to_connect(monkeypatch, storychat_env):
    from storychat_hermes import toolset_policy

    def broken(config=None):
        raise RuntimeError("unexpected detail")

    monkeypatch.setattr(toolset_policy, "unsafe_display_settings", broken)
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (
            "storychat_display_unverified", False)
        assert server.requests == []


async def test_a_bad_welcome_is_a_protocol_mismatch_with_the_detail_at_debug(monkeypatch, storychat_env,
                                                                            caplog):
    caplog.set_level(logging.DEBUG)
    from storychat_hermes import protocol

    def bad_welcome(raw):
        raise protocol.ProtocolError("welcome.userId must be a 24-hex ObjectId")

    monkeypatch.setattr(protocol, "parse_server_frame", bad_welcome)
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
    assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (
        "storychat_protocol_mismatch", False)
    assert any(r.levelno == logging.DEBUG and r.exc_info for r in caplog.records
               if r.name == "storychat_hermes.adapter")
