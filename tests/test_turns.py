import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio

from gateway.config import Platform
from gateway.platforms.event import ProcessingOutcome
from support import (CHAT_ID, MESSAGE_ID, TOKEN, TURN_ID, USER_ID, FakeStoryChat, make_adapter,
                     message_frame, wait_until, with_memory_provider)

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


async def test_a_message_hermes_does_not_admit_ends_the_turn_with_error(live):
    adapter, server = live

    async def not_admitted(event):  # Hermes leaves event._gateway_accepted False
        return None

    adapter.handle_message.side_effect = not_admitted
    await server.push("message", **message_frame())
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter._turns == {}


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


async def test_turn_is_refused_when_the_toolsets_changed_since_startup(monkeypatch, live, caplog):
    # config.yaml is re-read every turn (gateway/run_turn.py _load_gateway_config), so a config
    # edit after startup must not bypass the chat-only self-check (spec §9.3).
    adapter, server = live
    import hermes_cli.tools_config
    monkeypatch.setattr(hermes_cli.tools_config, "_get_platform_tools",
                        lambda config, platform, **kw: {"terminal"})
    await server.push("message", **message_frame())
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter.handle_message.await_count == 0
    assert adapter._turns == {}
    assert f"refused turn {TURN_ID}: the StoryChat toolsets changed since startup" in caplog.text


async def test_turn_is_refused_when_display_would_leak_progress_as_a_reply(monkeypatch, live, caplog):
    # Spec §9.2: a config edit after startup that turns tool_progress back on must not bypass the
    # connect-time display self-check either (config.yaml is re-read every turn, as above).
    adapter, server = live
    from storychat_hermes import toolset_policy
    monkeypatch.setattr(toolset_policy, "load_gateway_config", lambda: {})
    await server.push("message", **message_frame())
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter.handle_message.await_count == 0
    assert adapter._turns == {}
    assert f"refused turn {TURN_ID}: the StoryChat display settings changed since startup" in caplog.text


async def test_opt_in_turn_is_refused_when_hermes_approvals_turned_off(monkeypatch, storychat_env,
                                                                      caplog):
    import tools.approval_context
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "terminal")
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        try:
            monkeypatch.setattr(tools.approval_context, "_get_approval_config", lambda: {"mode": False})
            await server.push("message", **message_frame())
            assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                                 "chatId": CHAT_ID, "reason": "error"}
            assert adapter.handle_message.await_count == 0
            assert f"refused turn {TURN_ID}: Hermes approvals are off" in caplog.text
        finally:
            await adapter.disconnect()


async def test_turn_is_refused_when_a_memory_provider_was_turned_on(monkeypatch, live, caplog):
    adapter, server = live
    with_memory_provider(monkeypatch, "honcho")
    await server.push("message", **message_frame())
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter.handle_message.await_count == 0
    assert f"refused turn {TURN_ID}: an external memory provider is on" in caplog.text


async def test_turn_is_refused_when_the_recheck_cannot_run(monkeypatch, live, caplog):
    adapter, server = live
    from storychat_hermes import toolset_policy

    def broken():
        raise RuntimeError("unexpected detail")

    monkeypatch.setattr(toolset_policy, "load_gateway_config", broken)
    await server.push("message", **message_frame())
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter.handle_message.await_count == 0
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(TURN_ID in m and "RuntimeError" in m for m in errors)
    assert any(f"refused turn {TURN_ID}: could not verify the StoryChat toolsets/display settings" in m
               for m in errors)
    assert "unexpected detail" not in caplog.text


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


async def test_a_lone_surrogate_in_streamed_text_is_sent_not_raised(live):
    adapter, server = live
    await start_turn(adapter, server)
    sent = await adapter.send(CHAT_ID, "Ahoy \ud83d", reply_to=MESSAGE_ID)
    assert sent.success is True
    assert (await server.next_frame())["content"] == "Ahoy �"
    edited = await adapter.edit_message(CHAT_ID, sent.message_id, "Ahoy \ud83d!")
    assert edited.success is True
    assert (await server.next_frame())["content"] == "Ahoy �!"


async def test_interim_sends_are_status(live):
    adapter, server = live
    await start_turn(adapter, server)
    sent = await adapter.send(CHAT_ID, "Working…", metadata={"_interim_send": True})
    assert (await server.next_frame())["kind"] == "status"
    await adapter.edit_message(CHAT_ID, sent.message_id, "Still working…")
    assert (await server.next_frame())["kind"] == "status"


async def test_hermes_status_lines_are_status(live):
    # gateway/run.py _send_or_update_status_coro carries memory-recall, compression and context
    # warnings with no _interim_send marker; as kind "reply" the relay would save them as the
    # character's reply.
    adapter, server = live
    await start_turn(adapter, server)
    from gateway.run import _send_or_update_status_coro
    result = await _send_or_update_status_coro(adapter, CHAT_ID, "k", "🧠 recalled 3 memories", None)
    assert result.success is True
    frame = await server.next_frame()
    assert (frame["type"], frame["turnId"], frame["kind"], frame["content"]) == (
        "send", TURN_ID, "status", "🧠 recalled 3 memories")


async def test_platform_notices_are_status(live):
    # gateway/run_notifications.py _deliver_platform_notice: operational notices ("No home channel
    # is set", subagent failures) must not be saved as the character's reply either.
    adapter, server = live
    event = await start_turn(adapter, server)
    await deliver_platform_notice(adapter, event.source, "notice")
    frame = await server.next_frame()
    assert (frame["type"], frame["turnId"], frame["kind"], frame["content"]) == (
        "send", TURN_ID, "status", "notice")


async def deliver_platform_notice(adapter, source, content):
    from gateway.run import GatewayRunner
    runner = SimpleNamespace(_delivery_adapter_for=lambda s: adapter, config=None,
                             _thread_metadata_for_source=lambda s: None)
    await GatewayRunner._deliver_platform_notice(runner, source, content)


@pytest.mark.parametrize("marker", ["RECOVERED_MARKER", "RECONNECTED_MARKER", "FLOOD_MARKER"])
async def test_a_redelivered_reply_from_an_earlier_turn_is_status(live, marker):
    # gateway/delivery_ledger.py: a redelivered final carries the ledger's marker and no reply_to.
    # The plugin opts out of the ledger, but the boot sweep still redelivers a reply a crashed
    # gateway had saved but not sent, into whatever turn is running then.
    import gateway.delivery_ledger as ledger
    adapter, server = live
    event = await start_turn(adapter, server)
    assert (await adapter.send(CHAT_ID, getattr(ledger, marker) + "Reply A")).success is True
    assert (await server.next_frame())["kind"] == "status"
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert (await server.next_frame())["reason"] == "done"


async def test_gateway_warnings_are_status(live):
    # gateway/run_turn.py sends context-hygiene failures ("Shortening the conversation history
    # failed…") through BasePlatformAdapter.emit_warning, a plain send() with no marker.
    adapter, server = live
    await start_turn(adapter, server)
    result = await adapter.emit_warning(CHAT_ID, "⚠️ Shortening the conversation history failed.")
    assert result.success is True
    frame = await server.next_frame()
    assert (frame["type"], frame["turnId"], frame["kind"]) == ("send", TURN_ID, "status")
    media = await adapter.emit_media_warning(CHAT_ID, "(image could not be sent)")
    assert media.success is True
    assert (await server.next_frame())["kind"] == "status"


async def test_suppressed_gateway_warnings_are_not_sent(monkeypatch, live):
    adapter, server = live
    await start_turn(adapter, server)
    monkeypatch.setattr(adapter, "warning_notifications_enabled", lambda *a, **kw: False)
    assert await adapter.emit_warning(CHAT_ID, "⚠️ Shortening the conversation history failed.") is None
    assert server.frames.empty()


async def test_the_suspended_session_reset_notice_is_status_and_the_turn_goes_on(live):
    # gateway/run_turn.py _hmwa_deliver_auto_reset_notice: a plain send() before the real reply.
    from gateway.run import GatewayRunner
    adapter, server = live
    event = await start_turn(adapter, server)
    runner = SimpleNamespace(_delivery_adapter_for=lambda s: adapter,
                             _reset_notice_session_info=lambda s: None,
                             _thread_metadata_for_source=lambda s: None)
    await GatewayRunner._hmwa_deliver_auto_reset_notice(
        runner, SimpleNamespace(auto_reset_reason="suspended"), event.source, [])
    frame = await server.next_frame()
    assert (frame["type"], frame["kind"]) == ("send", "status")
    assert frame["content"].startswith("◐ Session reset")
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert (await server.next_frame())["reason"] == "done"


@pytest.mark.parametrize("configured", ["public", "pubilc"])
async def test_platform_notices_are_status_even_when_configured_public(monkeypatch, storychat_env,
                                                                       configured):
    # StoryChat has no public/private audience: "public" (explicit, or a typo Hermes normalises
    # to public) would send notices as the character's reply.
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter(extra={"notice_delivery": configured})
        assert await adapter.connect() is True
        try:
            event = await start_turn(adapter, server)
            await deliver_platform_notice(adapter, event.source, "notice")
            frame = await server.next_frame()
            assert (frame["type"], frame["kind"]) == ("send", "status")
        finally:
            await adapter.disconnect()


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


def _billing_text():
    from agent.conversation_loop import _billing_terminal_label
    return _billing_terminal_label("HTTP 402: insufficient credits", False)


def _overflow_text():
    from gateway.run import _CONTEXT_OVERFLOW_REPLY
    return _CONTEXT_OVERFLOW_REPLY


def _with_failed_notice(text, partial=False):
    from agent.turn_failure_copy import FAILED_TURN_NOTICE, PARTIAL_FAILED_TURN_NOTICE
    return f"{text}\n\n{PARTIAL_FAILED_TURN_NOTICE if partial else FAILED_TURN_NOTICE}"


# One per Hermes turn-failure template (Hermes still ends these turns as SUCCESS).
HERMES_FAILURE_TEXTS = [
    lambda: ("⚠️ Something went wrong and I couldn't finish this reply. Your sign-in to the AI model "
             "service has expired or the API key is wrong."),
    lambda: "⚠️ Your message was interrupted before processing started (likely by a recent /stop).",
    lambda: "⚠️ Your message wasn't processed (the previous turn was still being cleaned up).",
    lambda: "⚠️ Processing completed but no response was generated. Try sending it again.",
    lambda: "⚠️ I had to stop before finishing: processing incomplete. Use /retry to try again.",
    lambda: "⚠️ I couldn't connect to the AI model service, so this message wasn't processed.",
    _billing_text,
    _overflow_text,
    lambda: _with_failed_notice("OpenRouter rejected your API key, so the model can't be reached."),
    lambda: _with_failed_notice("The model service kept failing.", partial=True),
]


@pytest.mark.parametrize("failure", HERMES_FAILURE_TEXTS)
async def test_a_hermes_failure_text_is_status_and_ends_the_turn_with_error(live, failure):
    adapter, server = live
    event = await start_turn(adapter, server)
    text = failure()
    assert (await adapter.send(CHAT_ID, text, reply_to=MESSAGE_ID)).success is True
    frame = await server.next_frame()
    assert (frame["kind"], frame["content"]) == ("status", text)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "error"}


async def test_a_reply_that_only_mentions_a_failure_stays_the_reply(live):
    adapter, server = live
    event = await start_turn(adapter, server)
    text = "The captain frowns. \"⚠️ Something went wrong and I couldn't finish this reply,\" he reads."
    await adapter.send(CHAT_ID, text, reply_to=MESSAGE_ID)
    assert (await server.next_frame())["kind"] == "reply"
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert (await server.next_frame())["reason"] == "done"


async def test_untracked_events_send_no_turn_end(live):
    adapter, server = live
    event = await start_turn(adapter, server)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    await server.next_frame()
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)  # already ended: a no-op
    other = "f" * 32
    await server.push("message", **message_frame(turnId=other, userId="64b0000000000000000000ff"))
    # The next frame the relay sees must be THIS refusal, not a stray turn_end leaked by the no-op.
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": other,
                                         "chatId": CHAT_ID, "reason": "error"}


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


async def test_stop_dispatches_slash_stop_and_its_reply_is_status(live):
    adapter, server = live
    event = await start_turn(adapter, server)
    adapter._active_sessions[adapter._event_session_key(event)] = object()
    await server.push("stop", turnId=TURN_ID, chatId=CHAT_ID)
    await wait_until(lambda: adapter.handle_message.await_count == 2)
    stop_event = adapter.handle_message.await_args.args[0]
    assert (stop_event.text, stop_event.allow_gateway_control) == ("/stop", True)
    assert stop_event.message_id != MESSAGE_ID
    await adapter.send(CHAT_ID, "Stopped.", reply_to=stop_event.message_id,
                       metadata={"notify": True})
    assert (await server.next_frame())["kind"] == "status"
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert (await server.next_frame())["reason"] == "interrupted"


async def test_stop_for_an_idle_session_ends_the_turn_without_slash_stop(live):
    adapter, server = live
    await start_turn(adapter, server)
    await server.push("stop", turnId=TURN_ID, chatId=CHAT_ID)
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "interrupted"}
    assert adapter.handle_message.await_count == 1


async def test_a_second_stop_for_a_stopping_turn_is_ignored(live):
    adapter, server = live
    event = await start_turn(adapter, server)
    adapter._active_sessions[adapter._event_session_key(event)] = object()
    await server.push("stop", turnId=TURN_ID, chatId=CHAT_ID)
    await wait_until(lambda: adapter.handle_message.await_count == 2)
    await server.push("stop", turnId=TURN_ID, chatId=CHAT_ID)
    probe = "abcdefabcdefabcdefabcdefabcdefab"
    await server.push("message", **message_frame(turnId=probe, userId="64b0000000000000000000ff"))
    assert (await server.next_frame())["turnId"] == probe  # the second stop produced no frame
    assert adapter.handle_message.await_count == 2  # and no second /stop


async def test_stop_rechecks_that_hermes_is_busy_right_before_dispatching(live):
    # The /stop runs from a spawned task; if Hermes released the session in between, a /stop sent
    # anyway would reach an idle session (where it kills background processes).
    adapter, server = live
    event = await start_turn(adapter, server)
    session_key = adapter._event_session_key(event)
    adapter._active_sessions[session_key] = object()
    await adapter._on_stop({"turnId": TURN_ID, "chatId": CHAT_ID})
    del adapter._active_sessions[session_key]  # released before the spawned /stop runs
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "interrupted"}
    assert adapter.handle_message.await_count == 1
    assert adapter._turns == {}


async def test_stop_for_another_turn_is_ignored(live):
    adapter, server = live
    await start_turn(adapter, server)
    await server.push("stop", turnId="fedcba9876543210fedcba9876543210", chatId=CHAT_ID)
    probe = "abcdefabcdefabcdefabcdefabcdefab"
    await server.push("message", **message_frame(turnId=probe, userId="64b0000000000000000000ff"))
    assert (await server.next_frame())["turnId"] == probe  # the stop produced no frame first
    assert adapter.handle_message.await_count == 1


async def test_message_admitted_while_a_stopped_turn_unwinds_but_its_late_sends_are_refused(live):
    # Once B is admitted, A's OWN late sends (its "Stopped." reply, or a
    # reply_to its original messageId) must not attach to B's turnId/kind, or the backend would
    # save A's text as B's reply.
    adapter, server = live
    event = await start_turn(adapter, server)
    session_key = adapter._event_session_key(event)
    adapter._active_sessions[session_key] = object()
    a_chunk = await adapter.send(CHAT_ID, "A's first chunk", reply_to=MESSAGE_ID)
    await server.next_frame()
    await server.push("stop", turnId=TURN_ID, chatId=CHAT_ID)
    await wait_until(lambda: adapter.handle_message.await_count == 2)
    stop_event = adapter.handle_message.await_args.args[0]
    del adapter._active_sessions[session_key]  # Hermes released A's session
    turn_b, message_b_id = "e" * 32, "65c0000000000000000000d4"
    await server.push("message", **message_frame(turnId=turn_b, messageId=message_b_id))
    await wait_until(lambda: adapter.handle_message.await_count == 3)
    assert a_chunk.message_id not in adapter._msg_kinds  # A's bookkeeping went with A
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "interrupted"}

    stopped_reply = await adapter.send(CHAT_ID, "Stopped.", reply_to=stop_event.message_id)
    assert stopped_reply.success is False
    a_late_reply = await adapter.send(CHAT_ID, "A's final reply", reply_to=MESSAGE_ID)
    assert a_late_reply.success is False

    b_reply = await adapter.send(CHAT_ID, "B's reply", reply_to=message_b_id)
    assert b_reply.success is True
    assert await server.next_frame() == {"v": 1, "type": "send", "turnId": turn_b,
                                         "chatId": CHAT_ID, "msgId": b_reply.message_id,
                                         "content": "B's reply", "replyTo": message_b_id,
                                         "kind": "reply"}


async def test_stop_and_a_new_message_in_one_read_still_end_the_stopped_turn(live):
    # websockets hands over both frames without yielding, so B replaces the stopping A before the
    # spawned stop task runs; that task then finds A replaced and returns. No Hermes run will ever
    # end A (Hermes holds no session for it), so A's turn_end must be sent when B replaces it.
    adapter, server = live
    await start_turn(adapter, server)
    turn_b, message_b_id = "e" * 32, "65c0000000000000000000d4"
    await adapter._handle_frame(json.dumps({"v": 1, "type": "stop", "turnId": TURN_ID,
                                            "chatId": CHAT_ID}))
    await adapter._handle_frame(json.dumps({"v": 1, "type": "message", **message_frame(
        turnId=turn_b, messageId=message_b_id)}))
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "interrupted"}
    assert adapter.handle_message.await_count == 2
    assert adapter._turns[CHAT_ID].turn_id == turn_b
    probe = "abcdefabcdefabcdefabcdefabcdefab"
    await server.push("message", **message_frame(turnId=probe, userId="64b0000000000000000000ff"))
    assert (await server.next_frame())["turnId"] == probe  # A got exactly one turn_end


async def test_message_is_refused_while_hermes_still_runs_the_stopped_turn(live):
    # Admitted into a session Hermes still holds, B would hit Hermes' busy path: the busy ack
    # would go out as B's reply, B could be folded into A and never get turn_end, and A's
    # approval cards would carry B's turnId.
    adapter, server = live
    event = await start_turn(adapter, server)
    adapter._active_sessions[adapter._event_session_key(event)] = object()
    await server.push("stop", turnId=TURN_ID, chatId=CHAT_ID)
    await wait_until(lambda: adapter.handle_message.await_count == 2)
    turn_b, message_b_id = "e" * 32, "65c0000000000000000000d4"
    await server.push("message", **message_frame(turnId=turn_b, messageId=message_b_id))
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": turn_b,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter.handle_message.await_count == 2
    assert adapter._turns[CHAT_ID].turn_id == TURN_ID  # A is still tracked


async def test_message_is_refused_between_turn_end_and_hermes_releasing_the_session(live):
    # on_processing_complete runs before Hermes releases the session guard, so there is a short
    # gap where A is gone from the adapter but Hermes would still treat B as a busy follow-up.
    adapter, server = live
    event = await start_turn(adapter, server)
    adapter._active_sessions[adapter._event_session_key(event)] = object()
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert (await server.next_frame())["reason"] == "done"
    turn_b = "e" * 32
    await server.push("message", **message_frame(turnId=turn_b, messageId="65c0000000000000000000d4"))
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": turn_b,
                                         "chatId": CHAT_ID, "reason": "error"}
    assert adapter.handle_message.await_count == 1
    assert adapter._turns == {}


async def test_a_stale_hermes_session_lock_does_not_block_new_turns(live):
    # Hermes heals a guard whose owner task already exited when the next message arrives
    # (base.py _heal_stale_session_lock); refusing first must not skip that heal and trap the chat.
    adapter, server = live
    event = await start_turn(adapter, server)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    await server.next_frame()
    session_key = adapter._event_session_key(event)
    finished = asyncio.get_running_loop().create_future()
    finished.set_result(None)
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._session_tasks[session_key] = finished
    await server.push("message", **message_frame(turnId="e" * 32, messageId="65c0000000000000000000d4"))
    await wait_until(lambda: adapter.handle_message.await_count == 2)
    assert session_key not in adapter._active_sessions


async def test_send_reply_to_a_prior_chunk_in_the_same_turn_succeeds(live):
    adapter, server = live
    await start_turn(adapter, server)
    first = await adapter.send(CHAT_ID, "Chunk one")
    assert first.success is True
    await server.next_frame()
    second = await adapter.send(CHAT_ID, "Chunk two", reply_to=first.message_id)
    assert second.success is True
    assert await server.next_frame() == {"v": 1, "type": "send", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "msgId": second.message_id,
                                         "content": "Chunk two", "replyTo": first.message_id,
                                         "kind": "reply"}


async def test_stop_tracked_turns_skips_a_turn_that_already_ended(live):
    # A stale-snapshot race: _stop_tracked_turns spawns _dispatch_stop against today's turn, but it
    # runs a tick later. If the turn ended in between, it must not resurrect it or send /stop.
    adapter, server = live
    event = await start_turn(adapter, server)
    adapter._active_sessions[adapter._event_session_key(event)] = object()
    before = adapter.handle_message.await_count
    adapter._stop_tracked_turns()  # schedules _dispatch_stop against the current turn object
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)  # ends it first
    assert await server.next_frame() == {"v": 1, "type": "turn_end", "turnId": TURN_ID,
                                         "chatId": CHAT_ID, "reason": "done"}
    for _ in range(3):
        await asyncio.sleep(0)  # let the stale _dispatch_stop task run to completion
    assert adapter.handle_message.await_count == before  # no stray /stop was ever dispatched
    assert adapter._turns.get(CHAT_ID) is None  # and the ended turn was not resurrected


async def test_dispatch_stop_caps_stop_ids_at_256_entries():
    # _stop_ids is never pruned by Hermes' own lifecycle, so a long-running gateway must
    # cap it itself, or every /stop ever sent would be kept in memory forever.
    from storychat_hermes.adapter import MAX_STOP_IDS, _Turn
    adapter = make_adapter()
    first_stop_id = last_stop_id = None
    for i in range(MAX_STOP_IDS + 1):
        turn = _Turn(f"{i:032x}", CHAT_ID, MESSAGE_ID, "Mina", "Captain Rook")
        adapter._turns[CHAT_ID] = turn  # _dispatch_stop now requires the turn to still be tracked
        before = set(adapter._stop_ids)
        await adapter._dispatch_stop(turn)  # idle: no _active_sessions entry for this chat
        (new_id,) = set(adapter._stop_ids) - before
        if i == 0:
            first_stop_id = new_id
        last_stop_id = new_id
    assert len(adapter._stop_ids) == MAX_STOP_IDS
    assert first_stop_id not in adapter._stop_ids
    assert last_stop_id in adapter._stop_ids
