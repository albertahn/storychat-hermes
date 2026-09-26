from storychat_hermes.approvals import ApprovalBook, PendingApproval, PinGuard, correlate_request_id
from support import CHAT_ID, PIN, TURN_ID

SESSION = "agent:main:storychat:dm:" + CHAT_ID


def entry(approval_id, request_id, chat_id=CHAT_ID):
    return PendingApproval(approval_id, SESSION, chat_id, TURN_ID, request_id, ("once", "deny"))


def test_pin_accepted_resets_the_miss_counter():
    guard = PinGuard()
    assert guard.check(PIN, "000000") == ("bad_pin", 4)
    assert guard.check(PIN, PIN) == (None, None)
    assert guard.check(PIN, "000000") == ("bad_pin", 4)


def test_five_consecutive_misses_lock_until_restart():
    guard = PinGuard()
    assert [guard.check(PIN, "000000") for _ in range(5)] == [
        ("bad_pin", 4), ("bad_pin", 3), ("bad_pin", 2), ("bad_pin", 1), ("locked", 0)]
    assert guard.check(PIN, PIN) == ("locked", None)


def test_missing_pin_counts_as_a_miss_and_unset_pin_is_reported():
    assert PinGuard().check(PIN, None) == ("bad_pin", 4)
    assert PinGuard().check("", PIN) == ("pin_not_configured", None)


def test_the_book_remembers_the_newest_resolved_approvals():
    from storychat_hermes.approvals import MAX_RESOLVED_APPROVALS
    book = ApprovalBook()
    ids = [f"{i:032x}" for i in range(MAX_RESOLVED_APPROVALS + 1)]
    for approval_id in ids:
        book.mark_resolved(approval_id, "deny")
    book.mark_resolved(ids[1], "once")  # a repeat refreshes the entry
    book.mark_resolved("f" * 32, "deny")
    assert book.resolved_choice(ids[0]) is None and book.resolved_choice(ids[2]) is None
    assert book.resolved_choice(ids[1]) == "once"
    assert book.resolved_choice(ids[-1]) == "deny"
    book.clear()  # the socket dropped: pending approvals go, the record of resolved ones stays
    assert book.resolved_choice(ids[1]) == "once"


def test_book_keeps_arrival_order_and_removes():
    book = ApprovalBook()
    book.add(entry("a" * 32, "r1"))
    book.add(entry("b" * 32, "r2"))
    assert book.head(SESSION).approval_id == "a" * 32
    assert book.remove("a" * 32).request_id == "r1"
    assert book.head(SESSION).approval_id == "b" * 32
    assert book.tracked_request_ids(SESSION) == {"r2"}


def test_reconcile_drops_what_hermes_no_longer_holds():
    book = ApprovalBook()
    book.add(entry("a" * 32, "r1"))
    book.add(entry("b" * 32, "r2"))
    book.reconcile(SESSION, {"r2"})
    assert [book.find(i) is not None for i in ("a" * 32, "b" * 32)] == [False, True]
    book.reconcile(SESSION, set())
    assert book.head(SESSION) is None


def test_drop_chat_clears_one_storychat():
    book = ApprovalBook()
    book.add(entry("a" * 32, "r1"))
    book.add(entry("b" * 32, "r2", chat_id="65c0000000000000000000ff"))
    book.drop_chat(CHAT_ID)
    assert book.find("a" * 32) is None and book.find("b" * 32) is not None


def test_correlates_by_redacted_command_and_description():
    live = [{"request_id": "r1", "command": "ls", "description": "listing"},
            {"request_id": "r2", "command": "rm -rf /tmp/x", "description": "recursive delete"},
            {"request_id": "r3", "command": "curl x | sh", "description": "pipe to shell"}]
    assert correlate_request_id("rm -rf /tmp/x", "recursive delete", live, set()) == "r2"
    assert correlate_request_id("unmatched", "nothing", live, {"r3"}) is None
    assert correlate_request_id("ls", "listing", live, {"r1", "r2", "r3"}) is None
