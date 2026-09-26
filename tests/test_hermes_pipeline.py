"""The adapter driven through Hermes' real BasePlatformAdapter pipeline (no handle_message mock).

These catch drift in the plugin contract: handle_message → on_processing_complete, reply anchoring
and SendResult handling all come from the installed Hermes.
"""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from support import (CHAT_ID, MESSAGE_ID, TURN_ID, USER_ID, FakeStoryChat, make_adapter,
                     message_frame, wait_until)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def live(monkeypatch, storychat_env):
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        del adapter.handle_message  # use Hermes' own handle_message
        assert await adapter.connect() is True
        yield adapter, server
        await adapter.disconnect()


async def test_a_reply_then_turn_end_done(live):
    adapter, server = live

    async def agent(event):
        return f"Aye, {event.source.user_name}."

    adapter.set_message_handler(agent)
    await server.push("message", **message_frame())
    sent = await server.next_frame()
    assert (sent["type"], sent["kind"], sent["content"], sent["replyTo"]) == (
        "send", "reply", "Aye, Mina.", MESSAGE_ID)
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "done"}


def _failed_turn_reply():
    # gateway/run_turn.py appends the failed-turn notice to every failed turn's reply.
    from agent.turn_failure_copy import FAILED_TURN_NOTICE
    from gateway.run import GatewayRunner
    return GatewayRunner._hmwa_add_failed_turn_notice(None, "OpenRouter rejected your API key.",
                                                      FAILED_TURN_NOTICE)


def _normalized(agent_result):
    from gateway.run import _normalize_empty_agent_response
    return _normalize_empty_agent_response(agent_result, "", history_len=0)


# Hermes' own failure copy, produced by its real helpers so CI on Hermes main catches wording drift.
@pytest.mark.parametrize("failure", [
    lambda: _normalized({"failed": True, "error": "HTTP 401"}),
    lambda: _normalized({"interrupted": True, "api_calls": 0}),
    lambda: _normalized({"api_calls": 0}),
    lambda: _normalized({"api_calls": 2}),
    lambda: _normalized({"api_calls": 2, "partial": True, "error": "processing incomplete"}),
    _failed_turn_reply,
])
async def test_a_hermes_failure_is_status_and_the_turn_ends_with_error(live, failure):
    adapter, server = live
    text = failure()

    async def agent(event):
        return text

    adapter.set_message_handler(agent)
    await server.push("message", **message_frame())
    sent = await server.next_frame()
    assert (sent["type"], sent["kind"], sent["content"]) == ("send", "status", text)
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "error"}


async def test_a_message_hermes_drops_ends_the_turn_with_error(live, monkeypatch):
    # Hermes drops an event whose multiplexed profile route targets a profile this gateway does not
    # serve (base.py handle_message: _drop_unresolved). No on_processing_complete would ever end
    # the turn, so the relay would hold the chat busy for up to 14 minutes.
    adapter, server = live
    monkeypatch.setattr(adapter, "_drop_unresolved", lambda event: True)
    await server.push("message", **message_frame())
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter._turns == {}


async def test_chat_text_can_never_act_as_a_gateway_command(live):
    adapter, server = live
    seen = []

    async def agent(event):
        seen.append((event.text, event.get_command()))
        return "ok"

    adapter.set_message_handler(agent)
    await server.push("message", **message_frame(text="/yolo"))
    await server.next_frame()
    assert seen == [("/yolo", None)]


async def test_stop_interrupts_the_running_turn(live):
    adapter, server = live
    started = asyncio.Event()

    async def agent(event):
        if event.text == "/stop":
            return "Stopped."
        started.set()
        await asyncio.sleep(30)  # a long agent run; only the /stop cancellation ends it
        return "never sent"

    adapter.set_message_handler(agent)
    await server.push("message", **message_frame())
    await asyncio.wait_for(started.wait(), 3)
    await server.push("stop", turnId=TURN_ID, chatId=CHAT_ID)
    status = await server.next_frame()
    assert (status["type"], status["kind"], status["content"]) == ("send", "status", "Stopped.")
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "interrupted"}
    await wait_until(lambda: not adapter._active_sessions)


# Toolsets that can touch the host; none may reach StoryChat in chat-only mode (spec §9.3).
HOST_TOOLSETS = {"terminal", "file", "code_execution", "browser", "computer_use", "delegation"}


def runner_toolsets(adapter, *, with_adapter: bool = True) -> list:
    """What Hermes' own runner enables for a storychat turn (gateway/run_turn.py
    _resolve_enabled_toolsets_for_source), called the way _resolve_turn_toolsets calls it."""
    from gateway.run import GatewayRunner, _load_gateway_config, _platform_config_key
    source = adapter.build_source(chat_id=CHAT_ID, chat_name="Captain Rook", chat_type="dm",
                                  user_id=USER_ID, user_name="Mina")
    runner = SimpleNamespace(_delivery_adapter_for=lambda s: adapter if with_adapter else None)
    return GatewayRunner._resolve_enabled_toolsets_for_source(
        runner, _load_gateway_config(), source, _platform_config_key(source.platform))


async def connected_adapter(monkeypatch, server):
    monkeypatch.setenv("STORYCHAT_URL", server.url)
    adapter = make_adapter()
    assert await adapter.connect() is True
    return adapter


async def test_the_runner_resolves_chat_only_toolsets_for_storychat(monkeypatch, storychat_env):
    async with FakeStoryChat() as server:
        adapter = await connected_adapter(monkeypatch, server)
        try:
            # Control: without the adapter's override Hermes falls back to the full bundle, so the
            # assertions below can only pass because toolsets_for_source reached the runner.
            assert "terminal" in runner_toolsets(adapter, with_adapter=False)
            enabled = set(runner_toolsets(adapter))
            assert enabled <= {"x_search", "context_engine"}
            assert not enabled & HOST_TOOLSETS
        finally:
            await adapter.disconnect()


async def test_the_runner_resolves_opt_in_toolsets_for_storychat(monkeypatch, storychat_env):
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "web")
    async with FakeStoryChat() as server:
        adapter = await connected_adapter(monkeypatch, server)
        try:
            enabled = runner_toolsets(adapter)
            assert "web" in enabled
            assert "terminal" not in enabled
        finally:
            await adapter.disconnect()


async def test_a_failed_final_is_never_redelivered_into_the_next_turn(live):
    # Hermes' delivery ledger redelivers a final reply whose send failed (socket down) at +30 s and
    # +120 s, with no reply_to (gateway/run_startup.py _redeliver_claimed_obligations). By then a
    # later turn of the same chat may be running; the old reply must never be saved into it.
    import time

    from gateway.delivery_ledger import RECOVERED_MARKER, sweep_failed_for_runtime
    adapter, server = live
    real_send_frame = adapter._send_frame

    async def socket_down_for_reply_a(frame):
        return False if "Reply A" in frame else await real_send_frame(frame)

    adapter._send_frame = socket_down_for_reply_a
    release_b = asyncio.Event()

    async def agent(event):
        if event.message_id == MESSAGE_ID:
            return "Reply A"
        await release_b.wait()
        return "Reply B"

    adapter.set_message_handler(agent)
    await server.push("message", **message_frame())
    assert (await server.next_frame())["reason"] == "error"  # A's final never reached the relay
    adapter._send_frame = real_send_frame  # the socket is back
    await wait_until(lambda: not adapter._active_sessions)
    turn_b, message_b = "e" * 32, "65c0000000000000000000d4"
    await server.push("message", **message_frame(turnId=turn_b, messageId=message_b))
    await wait_until(lambda: adapter._active_sessions)
    # What the runner's redelivery timer does once A's retry is due.
    rows = sweep_failed_for_runtime("storychat", now=time.time() + 3600)
    for row in rows:
        marker = row.get("marker", RECOVERED_MARKER) if row.get("needs_marker") else ""
        await adapter.send(chat_id=row["chat_id"], content=marker + row["content"], metadata=None)
    release_b.set()
    frames = [await server.next_frame()]
    while frames[-1]["type"] != "turn_end":
        frames.append(await server.next_frame())
    assert [f["content"] for f in frames if f.get("kind") == "reply"] == ["Reply B"]
    assert frames[-1] == {"v": 1, "type": "turn_end", "turnId": turn_b, "chatId": CHAT_ID,
                          "reason": "done"}
    assert rows == []  # the adapter opted out of the ledger: nothing is ever redelivered
