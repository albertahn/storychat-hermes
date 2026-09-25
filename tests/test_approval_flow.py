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
        self.resolved.append((session_key, choice, request_id))
        before = len(self.live)
        self.live = [e for e in self.live if e["request_id"] != request_id]
        return before - len(self.live)


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
