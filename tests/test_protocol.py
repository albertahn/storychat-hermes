import json

import pytest

from storychat_hermes import protocol
from support import CHAT_ID, MESSAGE_ID, TURN_ID, USER_ID, message_frame

APPROVAL_ID = "fedcba9876543210fedcba9876543210"
MSG_ID = "00112233445566778899aabbccddeeff"


def test_hello_shape():
    assert json.loads(protocol.hello("0.1.0", ["x_search"])) == {
        "v": 1, "type": "hello", "pluginVersion": "0.1.0", "effectiveToolsets": ["x_search"]}


def test_send_and_edit_shapes():
    assert json.loads(protocol.send(TURN_ID, CHAT_ID, MSG_ID, "Hi", MESSAGE_ID, "reply")) == {
        "v": 1, "type": "send", "turnId": TURN_ID, "chatId": CHAT_ID, "msgId": MSG_ID,
        "content": "Hi", "replyTo": MESSAGE_ID, "kind": "reply"}
    assert json.loads(protocol.send(TURN_ID, CHAT_ID, MSG_ID, "…", None, "status"))["replyTo"] is None
    assert json.loads(protocol.edit(TURN_ID, CHAT_ID, MSG_ID, "Hi there", True, "reply")) == {
        "v": 1, "type": "edit", "turnId": TURN_ID, "chatId": CHAT_ID, "msgId": MSG_ID,
        "content": "Hi there", "final": True, "kind": "reply"}


def test_approval_shapes():
    assert json.loads(protocol.approval_request(
        TURN_ID, CHAT_ID, APPROVAL_ID, "rm -rf build", "recursive delete",
        ["once", "session", "always", "deny"], False, 1_700_000_000_000)) == {
        "v": 1, "type": "approval_request", "turnId": TURN_ID, "chatId": CHAT_ID,
        "approvalId": APPROVAL_ID, "command": "rm -rf build", "description": "recursive delete",
        "choices": ["once", "session", "always", "deny"], "smartDenied": False,
        "expiresAt": 1_700_000_000_000}
    assert json.loads(protocol.approval_ack(APPROVAL_ID, True)) == {
        "v": 1, "type": "approval_ack", "approvalId": APPROVAL_ID, "resolved": True}
    assert json.loads(protocol.approval_ack(APPROVAL_ID, False, "bad_pin", 3)) == {
        "v": 1, "type": "approval_ack", "approvalId": APPROVAL_ID, "resolved": False,
        "reason": "bad_pin", "attemptsLeft": 3}
    assert json.loads(protocol.approval_expired(APPROVAL_ID)) == {
        "v": 1, "type": "approval_expired", "approvalId": APPROVAL_ID}


def test_turn_end_shape():
    assert json.loads(protocol.turn_end(TURN_ID, CHAT_ID, "done")) == {
        "v": 1, "type": "turn_end", "turnId": TURN_ID, "chatId": CHAT_ID, "reason": "done"}


def test_parses_valid_server_frames():
    message = protocol.parse_server_frame(json.dumps({"v": 1, "type": "message", **message_frame()}))
    assert message["text"] == "Hello captain"
    welcome = protocol.parse_server_frame(json.dumps(
        {"v": 1, "type": "welcome", "connId": "a1b2c3d4-e5f6-4711-8899-aabbccddeeff", "userId": USER_ID}))
    assert welcome["userId"] == USER_ID
    decision = protocol.parse_server_frame(json.dumps(
        {"v": 1, "type": "approval_decision", "approvalId": APPROVAL_ID, "choice": "deny"}))
    assert decision.get("pin") is None
    stop = protocol.parse_server_frame(json.dumps(
        {"v": 1, "type": "stop", "turnId": TURN_ID, "chatId": CHAT_ID}))
    assert stop["turnId"] == TURN_ID
    upper = protocol.parse_server_frame(json.dumps(
        {"v": 1, "type": "stop", "turnId": TURN_ID.upper(), "chatId": CHAT_ID.upper()}))
    assert upper["chatId"] == CHAT_ID.upper()


@pytest.mark.parametrize("raw", [
    b"{}",
    "not json",
    "[]",
    json.dumps({"v": 2, "type": "stop", "turnId": TURN_ID, "chatId": CHAT_ID}),
    json.dumps({"v": 1, "type": "reboot"}),
    json.dumps({"v": 1, "type": "stop", "turnId": "short", "chatId": CHAT_ID}),
    json.dumps({"v": 1, "type": "message", **message_frame(chatId="not-an-object-id")}),
    json.dumps({"v": 1, "type": "message", **message_frame(text="")}),
    json.dumps({"v": 1, "type": "message", **message_frame(channelPrompt=None)}),
    json.dumps({"v": 1, "type": "approval_decision", "approvalId": APPROVAL_ID, "choice": "yolo"}),
    json.dumps({"v": 1, "type": "approval_decision", "approvalId": APPROVAL_ID, "choice": "once",
                "pin": 123456}),
])
def test_rejects_invalid_server_frames(raw):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_server_frame(raw)


def test_refuses_to_build_frames_over_one_mebibyte():
    with pytest.raises(protocol.FrameTooLarge):
        protocol.send(TURN_ID, CHAT_ID, MSG_ID, "x" * protocol.MAX_FRAME_BYTES, None, "reply")
