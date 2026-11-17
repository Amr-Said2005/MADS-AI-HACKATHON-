"""MOCKED delivery. A payment triggers delivery; an idempotency key prevents double delivery.

`payment_test` is the Phase 5 step that exercises the whole mocked sale-to-cash loop
and writes outputs/payment_test_log.md. It runs only after the founder approved the
payment test on its own at the Phase 5 milestone review, and uses that action id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import payments
import reconcile
import refunds
from decision_log import DecisionLog
from payments import MockLedger, Payment

if TYPE_CHECKING:
    from orchestrator import RunContext, StepResult

MOCKED = True


def idempotency_key(payment: Payment) -> str:
    return f"deliver:{payment.order_id}"


# Requirement R-SALE-TO-CASH (MOCKED): access is granted once per order under the idempotency key
# deliver:<order id>; a second call with the same key is skipped and returns False.
# Verified by: tests/test_sale_to_cash.py::test_a_duplicate_event_id_delivers_once
def deliver(ledger: MockLedger, payment: Payment, log: DecisionLog) -> bool:
    """Grant access once per order. Returns False if this key was already delivered."""
    key = idempotency_key(payment)
    if key in ledger.deliveries:
        ledger.events.append(f"delivery for {payment.order_id} skipped: already delivered (idempotency key {key})")
        log.record("delivery", "mocked_delivery_skipped_duplicate")
        return False
    ledger.deliveries[key] = payment.order_id
    ledger.access[payment.order_id] = True
    ledger.events.append(f"delivered {payment.order_id}: access granted (MOCKED)")
    log.record("delivery", "mocked_deliver")
    return True


def payment_test(ctx: "RunContext") -> "StepResult":
    from orchestrator import StepResult

    action_id = ctx.outbound_id("payment_test")  # @requires_approval below refuses it unless approved
    pricing = ctx.artifact("phase4_step1").get("data", {})
    price = float(pricing.get("price_usd") or 0) or 1.00
    product = str(pricing.get("product_name") or "digital product")
    ledger = MockLedger()
    log = ctx.log
    on_paid = lambda p: deliver(ledger, p, log)  # noqa: E731

    payments.open_checkout(ledger, approvals=ctx.approvals, action_id=action_id, log=log)
    a = payments.create_order(ledger, product, price, "test-customer-A", log)
    payments.receive_payment_event(ledger, "evt_A1", a.order_id, log, on_paid)
    payments.receive_payment_event(ledger, "evt_A1", a.order_id, log, on_paid)  # duplicate webhook
    b = payments.create_order(ledger, product, price, "test-customer-B", log)
    payments.receive_payment_event(ledger, "evt_B1", b.order_id, log, on_paid)
    refunds.refund(ledger, b.order_id, approvals=ctx.approvals, action_id=action_id, log=log)
    payments.mock_payout(ledger, log)
    report = reconcile.reconcile(ledger, log)

    checks = {
        "payment triggers delivery": ledger.access.get(a.order_id) is True,
        "duplicate event does not deliver twice": len([k for k in ledger.deliveries if k.endswith(a.order_id)]) == 1,
        "refund revokes access": ledger.access.get(b.order_id) is False,
        "reconciliation matches": report.matched,
    }
    lines = [
        "# Payment, delivery and refund test (MOCKED)",
        "",
        "> **MOCKED.** In-memory ledger only. No payment provider, no sandbox, no money moved, no email sent.",
        "",
        f"Product: {product}; test price: ${price:.2f}; mock fee: {payments.MOCK_FEE_RATE:.0%} + "
        f"${payments.MOCK_FEE_FIXED_USD:.2f} (illustrative, not a real provider's pricing).",
        f"Founder approval used: `{action_id}`",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "|---|---|",
        *[f"| {k} | {'pass' if v else 'FAIL'} |" for k, v in checks.items()],
        "",
        "## Events",
        "",
        *[f"{i}. {e}" for i, e in enumerate(ledger.events, 1)],
        "",
        "## Reconciliation",
        "",
        "| Order | Status | Amount | Paid | Fee | Refunded | Access |",
        "|---|---|---|---|---|---|---|",
        *[f"| {r['order']} | {r['status']} | {r['amount']:.2f} | {r['paid']:.2f} | {r['fee']:.2f} | "
          f"{r['refunded']:.2f} | {r['access']} |" for r in report.rows],
        "",
        f"Expected payout ${report.expected_payout:.2f}; recorded ${report.recorded_payout:.2f}; "
        f"{'matched' if report.matched else 'MISMATCH: ' + '; '.join(report.problems)}",
    ]
    return StepResult("\n".join(lines) + "\n", data={"checks": checks, "matched": report.matched})
