import logging

import pytest
import pytest_asyncio

from gateway.config import Platform
from gateway.platforms.event import ProcessingOutcome
from support import (CHAT_ID, MESSAGE_ID, TOKEN, TURN_ID, USER_ID, FakeStoryChat, make_adapter,
                     message_frame, wait_until)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def live(monkeypatch, storychat_env):
    """A connected adapter plus the fake relay it is talking to."""
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        yield adapter, server
        await adapter.disconnect()


async def start_turn(adapter, server, **overrides):
    await server.push("message", **message_frame(**overrides))
    await wait_until(lambda: adapter.handle_message.await_count >= 1)
    return adapter.handle_message.await_args.args[0]


async def test_message_frame_becomes_a_chat_only_event(live):
    adapter, server = live
    event = await start_turn(adapter, server)
    assert event.text == "Hello captain"
    assert event.message_id == MESSAGE_ID
    assert event.channel_prompt == "The following is character data for role-play."
    assert event.allow_gateway_control is False
    assert event.prompt_response is None
    assert event.source.platform == Platform("storychat")
    assert (event.source.chat_id, event.source.chat_type) == (CHAT_ID, "dm")
    assert (event.source.user_id, event.source.user_name) == (USER_ID, "Mina")
    assert event.source.chat_name == "Captain Rook"
    assert adapter._event_session_key(event) == f"agent:main:storychat:dm:{CHAT_ID}"


async def test_message_for_another_user_is_refused(live):
    adapter, server = live
    await server.push("message", **message_frame(userId="64b0000000000000000000ff"))
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter.handle_message.await_count == 0


async def test_second_message_while_a_turn_runs_is_refused(live):
    adapter, server = live
    await start_turn(adapter, server)
    other = "fedcba9876543210fedcba9876543210"
    await server.push("message", **message_frame(turnId=other))
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": other,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter.handle_message.await_count == 1


async def test_hermes_gets_the_user_id_as_spelled_in_the_allowlist(monkeypatch, storychat_env):
    # The relay sends lowercase ids; Hermes' own allowlist check is an exact string match.
    monkeypatch.setenv("STORYCHAT_ALLOWED_USERS", USER_ID.upper())
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        event = await start_turn(adapter, server)
        assert event.source.user_id == USER_ID.upper()
        await adapter.disconnect()


async def test_turn_is_refused_when_the_toolsets_changed_since_startup(monkeypatch, live):
    # config.yaml is re-read every turn (gateway/run_turn.py _load_gateway_config), so a config
    # edit after startup must not bypass the chat-only self-check (R-T12).
    adapter, server = live
    import hermes_cli.tools_config
    monkeypatch.setattr(hermes_cli.tools_config, "_get_platform_tools",
                        lambda config, platform, **kw: {"terminal"})
    await server.push("message", **message_frame())
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter.handle_message.await_count == 0
    assert adapter._turns == {}


async def test_reply_send_and_edit_strip_the_stream_cursor(live):
    adapter, server = live
    await start_turn(adapter, server)
    sent = await adapter.send(CHAT_ID, "Ahoy ▉", reply_to=MESSAGE_ID)
    assert sent.success is True
    assert await server.next_frame() == {"v": 1, "type": "send", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "msgId": sent.message_id,
                                         "content": "Ahoy", "replyTo": MESSAGE_ID, "kind": "reply"}
    edited = await adapter.edit_message(CHAT_ID, sent.message_id, "Ahoy, matey", finalize=True)
    assert edited.success is True
    assert await server.next_frame() == {"v": 1, "type": "edit", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "msgId": sent.message_id,
                                         "content": "Ahoy, matey", "final": True, "kind": "reply"}


async def test_interim_sends_are_status(live):
    adapter, server = live
    await start_turn(adapter, server)
    sent = await adapter.send(CHAT_ID, "Working…", metadata={"_interim_send": True})
    assert (await server.next_frame())["kind"] == "status"
    await adapter.edit_message(CHAT_ID, sent.message_id, "Still working…")
    assert (await server.next_frame())["kind"] == "status"


@pytest.mark.parametrize("outcome,reason", [(ProcessingOutcome.SUCCESS, "done"),
                                            (ProcessingOutcome.CANCELLED, "interrupted"),
                                            (ProcessingOutcome.FAILURE, "error")])
async def test_turn_end_maps_the_processing_outcome(live, outcome, reason):
    adapter, server = live
    event = await start_turn(adapter, server)
    await adapter.on_processing_complete(event, outcome)
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": reason}
    assert (await adapter.send(CHAT_ID, "after the end")).success is False


async def test_untracked_events_send_no_turn_end(live):
    adapter, server = live
    event = await start_turn(adapter, server)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    await server.next_frame()
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert server.frames.empty()


async def test_logs_never_carry_chat_text_or_the_channel_prompt(live, caplog):
    caplog.set_level(logging.DEBUG)
    adapter, server = live
    event = await start_turn(adapter, server)
    await adapter.send(CHAT_ID, "Secret reply text", reply_to=MESSAGE_ID)
    await server.next_frame()
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    await server.next_frame()
    for secret in (TOKEN, "Hello captain", "Secret reply text", "character data"):
        assert secret not in caplog.text
