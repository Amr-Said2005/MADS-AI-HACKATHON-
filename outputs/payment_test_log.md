> Phase 5: payment, delivery and refund test | Agent: delivery | Model: none (written by code) | Run started: 2026-11-17T19:39:36Z | Continued against a STOP recommendation at M1: someone already pays, claims mostly sourced

# Payment, delivery and refund test (MOCKED)

> **MOCKED.** In-memory ledger only. No payment provider, no sandbox, no money moved, no email sent.

Product: EE Study Hub; test price: $29.99; mock fee: 5% + $0.30 (illustrative, not a real provider's pricing).
Founder approval used: `payment_test:20261117T193936Z-an-educational-website-t-d1f9:9736961d06d0`

## Checks

| Check | Result |
|---|---|
| payment triggers delivery | pass |
| duplicate event does not deliver twice | pass |
| refund revokes access | pass |
| reconciliation matches | pass |

## Events

1. checkout opened (MOCKED)
2. order ord_0001 created for $29.99 (MOCKED)
3. payment evt_A1 for ord_0001: $29.99, fee $1.80 (MOCKED)
4. delivered ord_0001: access granted (MOCKED)
5. duplicate payment event evt_A1 ignored (MOCKED)
6. delivery for ord_0001 skipped: already delivered (idempotency key deliver:ord_0001)
7. order ord_0002 created for $29.99 (MOCKED)
8. payment evt_B1 for ord_0002: $29.99, fee $1.80 (MOCKED)
9. delivered ord_0002: access granted (MOCKED)
10. refund ord_0002 $29.99; access revoked (MOCKED)
11. payout recorded $26.39 (MOCKED)

## Reconciliation

| Order | Status | Amount | Paid | Fee | Refunded | Access |
|---|---|---|---|---|---|---|
| ord_0001 | paid | 29.99 | 29.99 | 1.80 | 0.00 | True |
| ord_0002 | refunded | 29.99 | 29.99 | 1.80 | 29.99 | False |

Expected payout $26.39; recorded $26.39; matched
