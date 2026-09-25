"""The adapter driven through Hermes' real BasePlatformAdapter pipeline (no handle_message mock).

These catch drift in the plugin contract: handle_message → on_processing_complete, reply anchoring
and SendResult handling all come from the installed Hermes.
"""

import asyncio

import pytest
import pytest_asyncio

from support import (CHAT_ID, MESSAGE_ID, TURN_ID, FakeStoryChat, make_adapter, message_frame,
                     wait_until)

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
