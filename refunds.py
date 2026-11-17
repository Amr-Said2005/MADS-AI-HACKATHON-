"""MOCKED refunds. A refund needs founder approval and revokes access to the product."""

from __future__ import annotations

from approval import ApprovalStore, requires_approval
from decision_log import DecisionLog
from payments import MockLedger

MOCKED = True


# Requirement R-SALE-TO-CASH (MOCKED): a refund of a paid order, once the founder approved its
# action id, marks the order refunded and sets its access to False. No money moves.
# Verified by: tests/test_sale_to_cash.py::test_a_refund_revokes_access
@requires_approval
def refund(ledger: MockLedger, order_id: str, *, approvals: ApprovalStore, action_id: str, log: DecisionLog) -> None:
    order = ledger.orders[order_id]
    if order.status != "paid":
        raise ValueError(f"order {order_id} is {order.status}, not paid")
    ledger.refunds[order_id] = order.amount_usd
    order.status = "refunded"
    ledger.access[order_id] = False
    ledger.events.append(f"refund {order_id} ${order.amount_usd:.2f}; access revoked (MOCKED)")
    log.record("refunds", "mocked_refund_and_revoke", approved_by="founder")
