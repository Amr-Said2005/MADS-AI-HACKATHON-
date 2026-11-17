import pytest

import delivery
import payments
import reconcile
import refunds
from agents import launcher
from approval import ApprovalRejected, ApprovalRequired, ApprovalStore, make_action_id, prompt_cli, requires_approval
from decision_log import DecisionLog

OUTBOUND = [payments.open_checkout, refunds.refund, launcher.publish_store_page, launcher.post_launch_post]


@pytest.fixture
def store(tmp_path):
    log = DecisionLog(tmp_path / "log.json")
    return ApprovalStore(tmp_path / "approvals.json", log), log


def test_every_outbound_function_is_gated():
    for fn in OUTBOUND:
        assert getattr(fn, "__requires_approval__", False), fn.__name__


def test_every_mocked_module_is_labelled():
    for m in (payments, delivery, refunds, reconcile, launcher):
        assert m.MOCKED is True


def test_outbound_call_without_approval_raises(store):
    s, log = store
    ledger = payments.MockLedger()
    aid = make_action_id("open_checkout", "run1", "x")
    with pytest.raises(ApprovalRequired):
        payments.open_checkout(ledger, approvals=s, action_id=aid, log=log)
    assert ledger.checkout_open is False
    assert aid in s.pending()  # the request is recorded for the founder


def test_rejected_action_raises(store):
    s, log = store
    aid = make_action_id("publish", "run1", "<html>")
    with pytest.raises(ApprovalRequired):
        launcher.publish_store_page("<html>", approvals=s, action_id=aid, log=log)
    s.decide(aid, approve=False, on_reject="stop")
    with pytest.raises(ApprovalRejected):
        launcher.publish_store_page("<html>", approvals=s, action_id=aid, log=log)


def test_approval_is_for_that_exact_action_only(store):
    s, log = store
    aid = make_action_id("publish", "run1", "<html>v1")
    other = make_action_id("publish", "run1", "<html>v2")
    assert aid != other
    s.request(aid, "publish v1")
    s.decide(aid, approve=True)
    assert launcher.publish_store_page("v1", approvals=s, action_id=aid, log=log).startswith("mock://")
    with pytest.raises(ApprovalRequired):
        launcher.publish_store_page("v2", approvals=s, action_id=other, log=log)


def test_approved_action_is_logged_with_founder(store):
    s, log = store
    aid = make_action_id("refund", "run1", "ord")
    ledger = payments.MockLedger(checkout_open=True)
    order = payments.create_order(ledger, "guide", 9.0, "test-customer", log)
    payments.receive_payment_event(ledger, "evt1", order.order_id, log)
    s.request(aid, "refund")
    s.decide(aid, approve=True)
    refunds.refund(ledger, order.order_id, approvals=s, action_id=aid, log=log)
    entries = log.entries()
    assert any(e["action"] == "mocked_refund_and_revoke" and e["approved_by"] == "founder" for e in entries)
    assert any(e["agent"] == "founder" and e["approved_by"] == "founder" for e in entries)
    assert all(e["model"] is None for e in entries if e["action"].startswith("mocked_"))


def test_decision_cannot_be_changed(store):
    s, _ = store
    s.request("a:b:c", "x")
    s.decide("a:b:c", approve=False)
    with pytest.raises(ValueError):
        s.decide("a:b:c", approve=True)


def test_decorator_requires_store_and_action_id():
    with pytest.raises(TypeError):
        @requires_approval
        def send_email(to):  # noqa: ANN001
            pass


def test_cli_prompt(store):
    s, _ = store
    s.request("x:1:a", "approve the page")
    assert prompt_cli(s, "x:1:a", input_fn=lambda _: "y") is True
    s.request("x:1:b", "approve the price")
    answers = iter(["n", "repeat"])
    assert prompt_cli(s, "x:1:b", input_fn=lambda _: next(answers)) is False
    assert s.record("x:1:b")["on_reject"] == "repeat"
