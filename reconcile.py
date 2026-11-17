"""MOCKED reconciliation: match orders, payments, fees, refunds and payouts in the mock ledger."""

from __future__ import annotations

from dataclasses import dataclass, field

from decision_log import DecisionLog
from payments import MockLedger

MOCKED = True


@dataclass
class Report:
    matched: bool
    rows: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    expected_payout: float = 0.0
    recorded_payout: float = 0.0


def reconcile(ledger: MockLedger, log: DecisionLog) -> Report:
    problems: list[str] = []
    rows = []
    paid_by_order: dict[str, float] = {}
    fees_by_order: dict[str, float] = {}
    for p in ledger.payments.values():
        paid_by_order[p.order_id] = paid_by_order.get(p.order_id, 0) + p.amount_usd
        fees_by_order[p.order_id] = fees_by_order.get(p.order_id, 0) + p.fee_usd
    for o in ledger.orders.values():
        paid = round(paid_by_order.get(o.order_id, 0.0), 2)
        refunded = round(ledger.refunds.get(o.order_id, 0.0), 2)
        rows.append({"order": o.order_id, "status": o.status, "amount": o.amount_usd, "paid": paid,
                     "fee": round(fees_by_order.get(o.order_id, 0.0), 2), "refunded": refunded,
                     "access": ledger.access.get(o.order_id, False)})
        if o.status in ("paid", "refunded") and paid != o.amount_usd:
            problems.append(f"{o.order_id}: paid {paid} but order is {o.amount_usd}")
        if o.status == "refunded" and ledger.access.get(o.order_id):
            problems.append(f"{o.order_id}: refunded but still has access")
        if o.status == "paid" and not ledger.access.get(o.order_id):
            problems.append(f"{o.order_id}: paid but not delivered")
    for oid in paid_by_order:
        if oid not in ledger.orders:
            problems.append(f"payment for unknown order {oid}")
    expected = round(sum(paid_by_order.values()) - sum(fees_by_order.values()) - sum(ledger.refunds.values()), 2)
    recorded = round(sum(ledger.payouts), 2)
    if abs(expected - recorded) > 0.005:
        problems.append(f"payout {recorded} does not match expected {expected}")
    log.record("reconcile", "mocked_reconcile", outcome="matched" if not problems else "mismatch")
    return Report(not problems, rows, problems, expected, recorded)
