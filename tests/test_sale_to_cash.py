"""MOCKED sale to cash: delivery, idempotency, refunds, fees, payouts and reconciliation (in-memory ledger)."""

import pytest

import delivery
import payments
import reconcile
import refunds
from approval import ApprovalRequired, ApprovalStore
from decision_log import DecisionLog
from payments import MockLedger


@pytest.fixture
def kit(tmp_path):
    log = DecisionLog(tmp_path / "log.json")
    store = ApprovalStore(tmp_path / "approvals.json", log)
    store.request("pay:test:1", "mocked payment test")
    store.decide("pay:test:1", approve=True)
    ledger = MockLedger()
    payments.open_checkout(ledger, approvals=store, action_id="pay:test:1", log=log)
    return ledger, store, log


def paid_order(ledger, log, event="evt_1", amount=10.00):
    order = payments.create_order(ledger, "Test product", amount, "test-customer", log)
    payments.receive_payment_event(ledger, event, order.order_id, log, lambda p: delivery.deliver(ledger, p, log))
    return order


def test_a_duplicate_event_id_delivers_once(kit):
    ledger, _, log = kit
    order = paid_order(ledger, log)
    again = payments.receive_payment_event(ledger, "evt_1", order.order_id, log, lambda p: delivery.deliver(ledger, p, log))
    assert again is False
    assert list(ledger.deliveries) == [f"deliver:{order.order_id}"] and len(ledger.payments) == 1
    assert ledger.access[order.order_id] is True


def test_a_refund_revokes_access(kit):
    ledger, store, log = kit
    order = paid_order(ledger, log)
    refunds.refund(ledger, order.order_id, approvals=store, action_id="pay:test:1", log=log)
    assert ledger.access[order.order_id] is False and order.status == "refunded"


def test_a_refund_needs_founder_approval(kit):
    ledger, store, log = kit
    order = paid_order(ledger, log)
    with pytest.raises(ApprovalRequired):
        refunds.refund(ledger, order.order_id, approvals=store, action_id="refund:not:approved", log=log)
    assert ledger.access[order.order_id] is True and not ledger.refunds


def test_a_refund_leaves_the_fee_unreturned_in_the_ledger(kit):
    ledger, store, log = kit
    order = paid_order(ledger, log, amount=10.00)
    fee = payments.mock_fee(10.00)
    refunds.refund(ledger, order.order_id, approvals=store, action_id="pay:test:1", log=log)
    payout = payments.mock_payout(ledger, log)
    assert ledger.refunds[order.order_id] == 10.00  # the customer gets the full amount back
    assert next(iter(ledger.payments.values())).fee_usd == fee  # the fee stays on the books
    assert payout == round(-fee, 2)  # so the refunded sale costs the seller the fee
    report = reconcile.reconcile(ledger, log)
    assert report.matched and report.rows[0]["fee"] == fee and report.rows[0]["refunded"] == 10.00


def test_an_unmatched_payout_is_flagged(kit):
    ledger, _, log = kit
    paid_order(ledger, log)
    payments.mock_payout(ledger, log)
    assert reconcile.reconcile(ledger, log).matched
    ledger.payouts.append(5.00)  # a payout that no payment explains
    report = reconcile.reconcile(ledger, log)
    assert not report.matched and any("payout" in p and "does not match" in p for p in report.problems)


def test_a_paid_order_that_was_not_delivered_is_flagged(kit):
    ledger, _, log = kit
    order = payments.create_order(ledger, "Test product", 10.00, "test-customer", log)
    payments.receive_payment_event(ledger, "evt_9", order.order_id, log)  # no delivery hook
    report = reconcile.reconcile(ledger, log)
    assert not report.matched and any("paid but not delivered" in p for p in report.problems)
