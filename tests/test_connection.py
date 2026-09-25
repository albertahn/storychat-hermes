import logging
import os
from pathlib import Path

import pytest

from support import CHAT_ID, TOKEN, FakeStoryChat, make_adapter

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


async def test_opt_in_warns_once_about_character_cards(monkeypatch, storychat_env, caplog):
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "web")
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        for _ in range(2):
            adapter = make_adapter()
            assert await adapter.connect() is True
            await adapter.disconnect()
        assert server.hellos[0]["effectiveToolsets"] == ["web"]
    warnings = [r for r in caplog.records if "character cards" in r.getMessage()]
    assert len(warnings) == 1


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


async def test_clarify_toolset_refuses_to_connect(monkeypatch, storychat_env):
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "coding")
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is False
        assert (adapter.fatal_error_code, adapter.fatal_error_retryable) == (
            "storychat_toolsets_clarify", False)
        assert server.requests == []
