"""MOCKED payments: an in-memory ledger. No payment provider, no SDK, not even sandbox.

Nothing here moves money. Opening checkout is an outbound action and needs the
founder's approval (@requires_approval) even though it is mocked.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from approval import ApprovalStore, requires_approval
from decision_log import DecisionLog

# Requirement R-MOCK-LABEL: this flag marks the module as a mock. Its ledger events end in
# "(MOCKED)", its log actions start with "mocked_", and the README status table names the module.
# Verified by: tests/test_structure.py::test_every_mocked_module_is_named_in_the_readme_table
MOCKED = True

# Illustrative mock fee so reconciliation has something to match. Not a real provider's pricing.
MOCK_FEE_RATE = 0.05
MOCK_FEE_FIXED_USD = 0.30


@dataclass
class Order:
    order_id: str
    product: str
    amount_usd: float
    customer_ref: str  # opaque test reference, never a name, email or card number
    status: str = "created"  # created -> paid -> refunded


@dataclass
class Payment:
    event_id: str
    order_id: str
    amount_usd: float
    fee_usd: float


# Requirement R-NO-MONEY (MOCKED): orders, payments, refunds and payouts are entries in this
# in-memory ledger only. No payment, mail or social client is among the project's dependencies.
# Verified by: tests/test_structure.py::test_no_payment_mail_or_social_client_in_dependencies
@dataclass
class MockLedger:
    checkout_open: bool = False
    orders: dict[str, Order] = field(default_factory=dict)
    payments: dict[str, Payment] = field(default_factory=dict)  # by event id
    refunds: dict[str, float] = field(default_factory=dict)  # order id -> amount
    access: dict[str, bool] = field(default_factory=dict)  # order id -> has access
    deliveries: dict[str, str] = field(default_factory=dict)  # idempotency key -> order id
    payouts: list[float] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1), repr=False)


def mock_fee(amount_usd: float) -> float:
    return round(amount_usd * MOCK_FEE_RATE + MOCK_FEE_FIXED_USD, 2)


@requires_approval
def open_checkout(ledger: MockLedger, *, approvals: ApprovalStore, action_id: str, log: DecisionLog) -> None:
    ledger.checkout_open = True
    ledger.events.append("checkout opened (MOCKED)")
    log.record("payments", "mocked_open_checkout", approved_by="founder")


def create_order(ledger: MockLedger, product: str, amount_usd: float, customer_ref: str, log: DecisionLog) -> Order:
    if not ledger.checkout_open:
        raise RuntimeError("checkout is not open")
    order = Order(f"ord_{next(ledger._ids):04d}", product, round(amount_usd, 2), customer_ref)
    ledger.orders[order.order_id] = order
    ledger.events.append(f"order {order.order_id} created for ${order.amount_usd:.2f} (MOCKED)")
    log.record("payments", "mocked_create_order")
    return order


# Requirement R-SALE-TO-CASH (MOCKED): a new payment event marks the order paid and calls the
# delivery hook; an event id seen before records no second payment. No real provider is called.
# Verified by: tests/test_sale_to_cash.py::test_a_duplicate_event_id_delivers_once
def receive_payment_event(ledger: MockLedger, event_id: str, order_id: str, log: DecisionLog, on_paid=None) -> bool:
    """A simulated provider webhook. Duplicate events are ignored. Returns True if newly recorded."""
    if event_id in ledger.payments:
        ledger.events.append(f"duplicate payment event {event_id} ignored (MOCKED)")
        log.record("payments", "mocked_duplicate_event_ignored")
        if on_paid:  # providers re-send webhooks; delivery must still be idempotent
            on_paid(ledger.payments[event_id])
        return False
    order = ledger.orders[order_id]
    pay = Payment(event_id, order_id, order.amount_usd, mock_fee(order.amount_usd))
    ledger.payments[event_id] = pay
    order.status = "paid"
    ledger.events.append(f"payment {event_id} for {order_id}: ${pay.amount_usd:.2f}, fee ${pay.fee_usd:.2f} (MOCKED)")
    log.record("payments", "mocked_payment_received")
    if on_paid:
        on_paid(pay)
    return True


def mock_payout(ledger: MockLedger, log: DecisionLog) -> float:
    """What a provider would pay out: payments minus fees minus refunds. Only recorded, never moved."""
    gross = sum(p.amount_usd for p in ledger.payments.values())
    fees = sum(p.fee_usd for p in ledger.payments.values())
    refunded = sum(ledger.refunds.values())
    amount = round(gross - fees - refunded, 2)
    ledger.payouts.append(amount)
    ledger.events.append(f"payout recorded ${amount:.2f} (MOCKED)")
    log.record("payments", "mocked_payout_recorded")
    return amount
