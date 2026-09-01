"""The reconciliation engine.

Two distinct jobs, deliberately kept apart because they are different shapes
of problem:

  1. Orders  <-> Razorpay payments   row to row, via a cascade of join keys
  2. Razorpay settlement <-> Bank    bundle to single line, via UTR + total

Everything here is deterministic Python. No model is consulted, because a
match rate is only meaningful if the matching is reproducible.

A row can be both matched and flagged. A late settlement, for example, is
matched with full confidence and still an exception, because the money is
not in this cycle's payout.
"""
from __future__ import annotations

import csv
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .models import (
    BankLine,
    Exception_,
    ExceptionCode,
    Match,
    Order,
    Payment,
    ReconResult,
    Tier,
)

# Tolerances for the weaker tiers.
AMOUNT_TOLERANCE = 1.00      # rupees
DATE_WINDOW_HOURS = 36
FEE_TOLERANCE = 0.50         # rupees; below this a fee gap is rounding
BANK_TOLERANCE = 1.00        # rupees


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_orders(path: str | Path) -> list[Order]:
    with Path(path).open(encoding="utf-8") as fh:
        return [
            Order(
                order_id=r["order_id"],
                amount=float(r["amount"]),
                created_at=r["created_at"],
                status=r["status"],
                customer_phone=r["customer_phone"],
            )
            for r in csv.DictReader(fh)
        ]


def load_payments(path: str | Path) -> list[Payment]:
    with Path(path).open(encoding="utf-8") as fh:
        return [
            Payment(
                payment_id=r["payment_id"],
                order_receipt=r["order_receipt"] or None,
                type=r["type"],
                amount=float(r["amount"]),
                fee=float(r["fee"]),
                tax=float(r["tax"]),
                settled_at=r["settled_at"],
                settlement_utr=r["settlement_utr"],
                method=r["method"],
                customer_phone=r.get("customer_phone", ""),
            )
            for r in csv.DictReader(fh)
        ]


def load_bank(path: str | Path) -> list[BankLine]:
    with Path(path).open(encoding="utf-8") as fh:
        return [
            BankLine(
                date=r["date"],
                utr=r["utr"],
                credit=float(r["credit"]),
                description=r["description"],
            )
            for r in csv.DictReader(fh)
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hours_apart(a: str, b: str) -> float:
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()) / 3600.0
    except ValueError:
        return float("inf")


def _expected_fee(amount: float, fee_rate: float, gst: float) -> tuple[float, float]:
    fee = round(amount * fee_rate + 1e-9, 2)
    return fee, round(fee * gst + 1e-9, 2)


def _net(p: Payment) -> float:
    """What this row contributes to the bank credit."""
    gross = p.amount if p.type == "payment" else -p.amount
    return round(gross - p.fee - p.tax, 2)


# ---------------------------------------------------------------------------
# Stage 1 — orders to payments
# ---------------------------------------------------------------------------

def _match_one(
    order: Order,
    by_receipt: dict[str, list[Payment]],
    by_amount: dict[float, list[Payment]],
    consumed: set[str],
    known_order_ids: set[str],
) -> tuple[Optional[Payment], Tier, str, list[Payment]]:
    """Run the cascade for a single order.

    Returns the payment, the tier that found it, a sentence stating the rule
    that fired, and — when a weak tier found several equally good candidates —
    the tied rows.

    The tie case matters. Two orders of the same value, in the same window,
    with no receipt and no phone are genuinely indistinguishable: no rule can
    separate them because the distinguishing information was never recorded.
    Picking one would be a coin flip dressed up as a match, so the engine
    declines and escalates instead.
    """
    # Tier 1 — the merchant's own id, echoed back by Razorpay. Definitive.
    for p in by_receipt.get(order.order_id, []):
        if p.payment_id not in consumed:
            return p, Tier.RECEIPT, f"order_receipt == {order.order_id}", []

    # Tier 3 — no usable receipt. Amount, timing and phone together are
    # strong circumstantial evidence.
    t3 = [
        p for p in by_amount.get(round(order.amount, 2), [])
        if p.payment_id not in consumed
        and p.customer_phone
        and p.customer_phone == order.customer_phone
        and _hours_apart(order.created_at, p.settled_at) <= DATE_WINDOW_HOURS
    ]
    if len(t3) == 1:
        return t3[0], Tier.AMOUNT_PHONE, (
            f"amount {order.amount:.2f} + phone {order.customer_phone} "
            f"within {DATE_WINDOW_HOURS}h"
        ), []
    if len(t3) > 1:
        return None, Tier.UNMATCHED, "several rows share amount, phone and window", t3

    # Tier 4 — amount alone, within tolerance and window. Weak; always flagged.
    #
    # A payment carrying a receipt that names some *other* real order is off
    # limits here. Without that guard an amount collision lets one order steal
    # another's payment, which then cascades: the robbed order is reported as
    # a phantom and the thief's true exception is never raised.
    t4: list[Payment] = []
    for cand_amt, rows in by_amount.items():
        if abs(cand_amt - order.amount) > AMOUNT_TOLERANCE:
            continue
        for p in rows:
            if p.payment_id in consumed:
                continue
            if p.order_receipt and p.order_receipt != order.order_id and p.order_receipt in known_order_ids:
                continue
            if _hours_apart(order.created_at, p.settled_at) <= DATE_WINDOW_HOURS:
                t4.append(p)
    if len(t4) == 1:
        return t4[0], Tier.AMOUNT_ONLY, (
            f"amount {order.amount:.2f} within ±{AMOUNT_TOLERANCE:.2f}, "
            f"no receipt or phone corroboration"
        ), []
    if len(t4) > 1:
        return None, Tier.UNMATCHED, (
            f"{len(t4)} rows match on amount alone; nothing separates them"
        ), t4

    return None, Tier.UNMATCHED, "no payment found by any rule", []


def reconcile(
    orders: Iterable[Order],
    payments: Iterable[Payment],
    bank: Iterable[BankLine],
    *,
    settlement_utr: str,
    fee_rate: float = 0.02,
    gst_on_fee: float = 0.18,
) -> ReconResult:
    """Reconcile one settlement cycle."""
    started = time.perf_counter()

    orders = list(orders)
    payments = list(payments)
    bank = list(bank)

    by_receipt: dict[str, list[Payment]] = defaultdict(list)
    by_amount: dict[float, list[Payment]] = defaultdict(list)
    for p in payments:
        if p.order_receipt:
            by_receipt[p.order_receipt].append(p)
        by_amount[round(p.amount, 2)].append(p)

    known_order_ids = {o.order_id for o in orders}

    result = ReconResult(orders_seen=len(orders), payments_seen=len(payments))
    consumed: set[str] = set()
    # Rows already accounted for by an ambiguity. They are unmatched, but they
    # are not orphans — the AMBIGUOUS_MATCH exception already names them, and
    # reporting them again would double-count the same rupees.
    contested: set[str] = set()

    for order in orders:
        payment, tier, rule, tied = _match_one(
            order, by_receipt, by_amount, consumed, known_order_ids
        )

        if payment is None and tied:
            # Several candidates, nothing to choose between them. Refusing is
            # the correct answer: a wrong pairing corrupts two rows, not one.
            result.exceptions.append(Exception_.build(
                ExceptionCode.AMBIGUOUS_MATCH,
                order_id=order.order_id,
                amount_at_risk=order.amount,
                detail=(f"{len(tied)} settled rows are equally consistent with "
                        f"{order.order_id} (₹{order.amount:,.2f}). {rule}. "
                        f"No rule can separate them — escalated rather than guessed."),
                possible_causes=[
                    "Two customers paid identical amounts in the same window",
                    "The order receipt was never recorded against either payment",
                    "A duplicate charge the shop has not noticed",
                ],
                evidence={"candidates": [p.payment_id for p in tied],
                          "order_amount": order.amount,
                          "rule_attempted": rule},
            ))
            contested.update(p.payment_id for p in tied)
            continue

        if payment is None:
            # No settled row exists for this order *today*. Whether that is a
            # phantom or merely a late settlement is not knowable from this
            # cycle alone: tomorrow's report does not exist yet. Naming one
            # cause would be a guess, so the finding is carried forward and
            # the next cycle decides it.
            result.exceptions.append(Exception_.build(
                ExceptionCode.UNRESOLVED_NO_ROW,
                order_id=order.order_id,
                amount_at_risk=order.amount,
                detail=(f"Order {order.order_id} is marked {order.status} for "
                        f"₹{order.amount:,.2f}, but no settled row matches it in "
                        f"this cycle. Carried forward — the next cycle will "
                        f"resolve or confirm it."),
                possible_causes=[
                    "Settles in a later cycle and is simply not in today's report",
                    "Razorpay is holding the payment for risk review",
                    "Refunded without the shop's system being updated",
                    "Payment failed and was wrongly marked paid — goods may have "
                    "shipped without payment",
                ],
                evidence={"order": order.__dict__, "rule_attempted": rule},
            ))
            continue

        consumed.add(payment.payment_id)
        result.matches.append(Match(
            order_id=order.order_id,
            payment_id=payment.payment_id,
            tier=tier,
            order_amount=order.amount,
            payment_amount=payment.amount,
            fee=payment.fee,
            tax=payment.tax,
            net_to_bank=_net(payment),
            settlement_utr=payment.settlement_utr,
            rule=rule,
        ))

        # ---- checks that apply to a successfully matched pair ------------
        # Razorpay levies its fee on the original captured amount, so a refund
        # row still carries the full fee while `amount` shows only the portion
        # returned. Comparing against the payment amount would flag every
        # partial refund as an overcharge, so the order amount is the correct
        # basis.
        fee_basis = order.amount if payment.type != "payment" else payment.amount
        exp_fee, exp_tax = _expected_fee(fee_basis, fee_rate, gst_on_fee)
        overcharge = (payment.fee + payment.tax) - (exp_fee + exp_tax)

        if payment.type == "chargeback":
            result.exceptions.append(Exception_.build(
                ExceptionCode.CHARGEBACK,
                order_id=order.order_id, payment_id=payment.payment_id,
                amount_at_risk=payment.amount,
                detail=(f"₹{payment.amount:,.2f} was pulled back by the customer's "
                        f"bank. Goods likely already delivered."),
                possible_causes=["Customer disputed the charge",
                                 "Fraudulent transaction",
                                 "Goods not received or not as described"],
                evidence={"payment_id": payment.payment_id, "method": payment.method},
            ))

        elif payment.type == "refund":
            partial = payment.amount < order.amount - 0.01
            code = ExceptionCode.PARTIAL_REFUND if partial else ExceptionCode.FULL_REFUND
            result.exceptions.append(Exception_.build(
                code,
                order_id=order.order_id, payment_id=payment.payment_id,
                amount_at_risk=(order.amount - payment.amount) if partial else order.amount,
                detail=(f"Refund of ₹{payment.amount:,.2f} against an order of "
                        f"₹{order.amount:,.2f}."),
                evidence={"order_amount": order.amount, "refund_amount": payment.amount},
            ))

        if overcharge > FEE_TOLERANCE:
            actual_rate = payment.fee / fee_basis * 100 if fee_basis else 0
            result.exceptions.append(Exception_.build(
                ExceptionCode.FEE_MISMATCH,
                order_id=order.order_id, payment_id=payment.payment_id,
                amount_at_risk=overcharge,
                detail=(f"Fee charged at {actual_rate:.2f}% against an agreed "
                        f"{fee_rate * 100:.2f}%. Overcharged ₹{overcharge:,.2f}."),
                evidence={"charged_fee": payment.fee, "expected_fee": exp_fee,
                          "charged_tax": payment.tax, "expected_tax": exp_tax},
            ))

        if payment.settlement_utr != settlement_utr:
            result.exceptions.append(Exception_.build(
                ExceptionCode.LATE_SETTLEMENT,
                order_id=order.order_id, payment_id=payment.payment_id,
                amount_at_risk=payment.amount,
                detail=(f"Paid, but settles under {payment.settlement_utr} rather "
                        f"than this cycle's {settlement_utr}. Expected to clear "
                        f"on the next run."),
                evidence={"settled_at": payment.settled_at,
                          "settlement_utr": payment.settlement_utr},
            ))

        if tier in (Tier.AMOUNT_PHONE, Tier.AMOUNT_ONLY):
            result.exceptions.append(Exception_.build(
                ExceptionCode.MISSING_RECEIPT,
                order_id=order.order_id, payment_id=payment.payment_id,
                amount_at_risk=0.0,
                detail=(f"Matched at {tier.value} "
                        f"({tier.confidence:.0%} confidence) because no order "
                        f"receipt was recorded against the payment."),
                evidence={"rule": rule},
            ))

    # ---- payments nothing claimed ---------------------------------------
    seen_receipts: dict[str, int] = defaultdict(int)
    for p in payments:
        if p.order_receipt:
            seen_receipts[p.order_receipt] += 1

    order_ids = known_order_ids
    order_amounts = {o.order_id: o.amount for o in orders}
    for p in payments:
        if p.payment_id in consumed or p.payment_id in contested:
            continue
        if p.order_receipt and p.order_receipt in order_ids and seen_receipts[p.order_receipt] > 1:
            # Two rows against one order look identical at a glance. They are
            # told apart by arithmetic: parts that SUM to the order value are a
            # legitimate split capture, parts that each EQUAL it are a double
            # charge. Nothing in the data says which — it has to be worked out.
            siblings = [q for q in payments
                        if q.order_receipt == p.order_receipt and q.type == "payment"]
            total = round(sum(q.amount for q in siblings), 2)
            expected = round(order_amounts.get(p.order_receipt, 0.0), 2)

            if expected and abs(total - expected) <= AMOUNT_TOLERANCE:
                result.exceptions.append(Exception_.build(
                    ExceptionCode.SPLIT_CAPTURE,
                    order_id=p.order_receipt, payment_id=p.payment_id,
                    amount_at_risk=0.0,
                    detail=(f"{len(siblings)} rows against {p.order_receipt} sum to "
                            f"₹{total:,.2f}, matching the order value. A part "
                            f"capture, not a double charge — no money at risk."),
                    evidence={"parts": [q.amount for q in siblings],
                              "order_amount": expected, "sum": total},
                ))
            else:
                result.exceptions.append(Exception_.build(
                    ExceptionCode.DUPLICATE_SETTLEMENT,
                    order_id=p.order_receipt, payment_id=p.payment_id,
                    amount_at_risk=p.amount,
                    detail=(f"{len(siblings)} rows against {p.order_receipt} total "
                            f"₹{total:,.2f} against an order of ₹{expected:,.2f}. "
                            f"₹{p.amount:,.2f} looks to have been taken twice."),
                    evidence={"parts": [q.amount for q in siblings],
                              "order_amount": expected, "sum": total},
                ))
        else:
            result.exceptions.append(Exception_.build(
                ExceptionCode.ORPHAN_PAYMENT,
                payment_id=p.payment_id,
                amount_at_risk=p.amount,
                detail=(f"Razorpay settled ₹{p.amount:,.2f} under receipt "
                        f"'{p.order_receipt or '(blank)'}', which does not exist "
                        f"in the shop's order list."),
                possible_causes=["Order placed through a channel not in this export",
                                 "Receipt reference was mistyped at checkout",
                                 "Order deleted from the shop's system after payment"],
                evidence={"payment_id": p.payment_id, "receipt": p.order_receipt},
            ))

    # ---- Stage 2 — settlement bundle against the bank line ---------------
    result.exceptions.extend(_check_settlements(payments, bank))

    result.elapsed_seconds = time.perf_counter() - started
    return result


# ---------------------------------------------------------------------------
# Stage 2 — bundle to bank line
# ---------------------------------------------------------------------------

def _check_settlements(payments: list[Payment], bank: list[BankLine]) -> list[Exception_]:
    """Prove each bank credit equals the net of the rows carrying its UTR.

    The bank never sees orders — only a lump sum. The only usable join key
    is the UTR, and the only usable check is arithmetic.
    """
    out: list[Exception_] = []
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for p in payments:
        totals[p.settlement_utr] += _net(p)
        counts[p.settlement_utr] += 1

    for line in bank:
        expected = round(totals.get(line.utr, 0.0), 2)
        gap = round(line.credit - expected, 2)
        if abs(gap) > BANK_TOLERANCE:
            out.append(Exception_.build(
                ExceptionCode.SETTLEMENT_SHORTFALL,
                amount_at_risk=abs(gap),
                detail=(f"Bank credited ₹{line.credit:,.2f} under {line.utr}, but "
                        f"the {counts.get(line.utr, 0)} settled rows net to "
                        f"₹{expected:,.2f}. Unexplained gap ₹{gap:,.2f}."),
                possible_causes=["A settled row is missing from the recon report",
                                 "An adjustment was applied outside the report",
                                 "Bank fee deducted on credit"],
                evidence={"utr": line.utr, "bank_credit": line.credit,
                          "computed_net": expected, "rows": counts.get(line.utr, 0)},
            ))
    return out
