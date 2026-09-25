import random

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from storychat_hermes import lifecycle


@pytest.mark.parametrize("close_code,retry,start,code", [
    (4001, False, 0.0, "storychat_replaced"),
    (4003, False, 0.0, "storychat_token_rejected"),
    (4400, False, 0.0, "storychat_protocol_mismatch"),
    (1009, False, 0.0, "storychat_protocol_mismatch"),
    (4429, True, 60.0, "storychat_rate_limited"),
    (1011, True, 1.0, "storychat_disconnected"),
    (1001, True, 1.0, "storychat_disconnected"),
    (1006, True, 1.0, "storychat_disconnected"),
    (None, True, 1.0, "storychat_disconnected"),
])
def test_close_code_table(close_code, retry, start, code):
    verdict = lifecycle.classify_close(close_code)
    assert (verdict.retry, verdict.backoff_start, verdict.code) == (retry, start, code)


@pytest.mark.parametrize("status,retry,start", [
    (301, False, 0.0), (302, False, 0.0), (307, False, 0.0), (308, False, 0.0),
    (401, False, 0.0), (429, True, 60.0), (404, True, 1.0), (502, True, 1.0),
])
def test_upgrade_status_table(status, retry, start):
    verdict = lifecycle.classify_upgrade_status(status)
    assert (verdict.retry, verdict.backoff_start) == (retry, start)


def test_log_lines_are_the_spec_wording():
    assert lifecycle.classify_close(4001).message == "another client took over — rotate your token"
    assert lifecycle.classify_close(4003).message == (
        "token invalid or revoked — generate a new one at storychat.app/chat/hermes")
    assert lifecycle.classify_upgrade_status(401).message == lifecycle.classify_close(4003).message
    assert lifecycle.classify_close(4400).message == "protocol mismatch — update storychat-hermes"
    assert lifecycle.classify_close(1009).message == "protocol mismatch — update storychat-hermes"
    assert lifecycle.classify_upgrade_status(302).message == "unexpected redirect — check STORYCHAT_URL"


def test_backoff_grows_from_one_second_to_sixty_with_jitter():
    rng = random.Random(7)
    for attempt, base in enumerate([1, 2, 4, 8, 16, 32, 60, 60]):
        assert base <= lifecycle.backoff_delay(attempt, 1.0, rng) <= base * 1.25


def test_rate_limited_backoff_never_drops_below_sixty_seconds():
    rng = random.Random(7)
    assert all(60 <= lifecycle.backoff_delay(n, 60.0, rng) <= 75 for n in range(5))


def test_redirects_are_never_followed():
    redirect = InvalidStatus(Response(302, "Found", Headers({"Location": "wss://elsewhere.test/"}), b""))
    connector = lifecycle.NoRedirectConnect("wss://prod-rail.storychat.app/api/v1/hermes/connect")
    assert connector.process_redirect(redirect) is redirect
