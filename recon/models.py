"""Core data shapes for the reconciliation engine.

Three input records (order, payment, bank line) and two output records
(a match, or an exception). Everything the engine produces is one of these,
so the audit trail is just a list of them.

Design rule carried over from prior work: these objects are produced by
deterministic Python only. The LLM layer reads them and never writes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Inputs — one class per source file
# ---------------------------------------------------------------------------

@dataclass
class Order:
    """A row from the merchant's own order system."""
    order_id: str
    amount: float          # rupees, what the shop believes it sold
    created_at: str        # ISO date
    status: str            # PAID / REFUNDED / FAILED
    customer_phone: str


@dataclass
class Payment:
    """A row from Razorpay's settlement recon report.

    `order_receipt` is the merchant's own order id, echoed back by Razorpay
    because the merchant passed it when creating the order. It is the primary
    join key — when it is present and correct.
    """
    payment_id: str
    order_receipt: Optional[str]   # merchant's order id — may be blank in practice
    type: str                      # payment / refund / chargeback
    amount: float
    fee: float
    tax: float
    settled_at: str
    settlement_utr: str
    method: str
    customer_phone: str = ""


@dataclass
class BankLine:
    """A single credit on the bank statement — one lump per settlement."""
    date: str
    utr: str
    credit: float
    description: str


# ---------------------------------------------------------------------------
# Matching outcome
# ---------------------------------------------------------------------------

class Tier(str, Enum):
    """How a match was made. Lower tiers are stronger evidence.

    Recorded on every match so the audit trail can answer
    "why do you believe these two rows are the same transaction?"
    """
    RECEIPT      = "T1_RECEIPT"        # exact order_receipt match
    PAYMENT_ID   = "T2_PAYMENT_ID"     # shop stored Razorpay's payment_id
    AMOUNT_PHONE = "T3_AMOUNT_PHONE"   # amount + date window + phone
    AMOUNT_ONLY  = "T4_AMOUNT_ONLY"    # amount alone within tolerance
    UNMATCHED    = "T5_UNMATCHED"

    @property
    def confidence(self) -> float:
        return {
            Tier.RECEIPT: 1.00,
            Tier.PAYMENT_ID: 1.00,
            Tier.AMOUNT_PHONE: 0.85,
            Tier.AMOUNT_ONLY: 0.60,
            Tier.UNMATCHED: 0.00,
        }[self]


class ExceptionCode(str, Enum):
    """Why a row could not be cleanly reconciled.

    These are the labels the merchant actually acts on, so each one names a
    real-world cause rather than a technical failure.
    """
    LATE_SETTLEMENT      = "LATE_SETTLEMENT"       # paid, but settles next cycle
    FEE_MISMATCH         = "FEE_MISMATCH"          # fee differs from agreed rate
    PARTIAL_REFUND       = "PARTIAL_REFUND"        # refunded in part
    FULL_REFUND          = "FULL_REFUND"           # refunded entirely
    CHARGEBACK           = "CHARGEBACK"            # bank pulled the money back
    MISSING_RECEIPT      = "MISSING_RECEIPT"       # matched, but only via weak rule
    DUPLICATE_SETTLEMENT = "DUPLICATE_SETTLEMENT"  # same payment settled twice
    ORPHAN_PAYMENT       = "ORPHAN_PAYMENT"        # in Razorpay, not in shop's orders
    PHANTOM_ORDER        = "PHANTOM_ORDER"         # shop says PAID, no payment exists
    SETTLEMENT_SHORTFALL = "SETTLEMENT_SHORTFALL"  # bundle total != bank credit
    AMBIGUOUS_MATCH      = "AMBIGUOUS_MATCH"       # several equally good candidates
    UNRESOLVED_NO_ROW    = "UNRESOLVED_NO_ROW"     # no settled row yet; cause unknowable today
    SPLIT_CAPTURE        = "SPLIT_CAPTURE"         # several rows that legitimately sum to the order


# Severity drives ordering in the exception queue: money at risk first.
SEVERITY: dict[ExceptionCode, str] = {
    ExceptionCode.PHANTOM_ORDER:        "CRITICAL",
    ExceptionCode.CHARGEBACK:           "CRITICAL",
    ExceptionCode.SETTLEMENT_SHORTFALL: "CRITICAL",
    ExceptionCode.AMBIGUOUS_MATCH:      "HIGH",
    ExceptionCode.UNRESOLVED_NO_ROW:    "HIGH",
    ExceptionCode.SPLIT_CAPTURE:        "LOW",
    ExceptionCode.ORPHAN_PAYMENT:       "HIGH",
    ExceptionCode.DUPLICATE_SETTLEMENT: "HIGH",
    ExceptionCode.FEE_MISMATCH:         "HIGH",
    ExceptionCode.PARTIAL_REFUND:       "MEDIUM",
    ExceptionCode.FULL_REFUND:          "LOW",
    ExceptionCode.LATE_SETTLEMENT:      "LOW",
    ExceptionCode.MISSING_RECEIPT:      "LOW",
}

# Exceptions that typically resolve themselves on the next cycle's run.
SELF_RESOLVING: frozenset[ExceptionCode] = frozenset({
    ExceptionCode.LATE_SETTLEMENT,
    ExceptionCode.UNRESOLVED_NO_ROW,
})

# How a check reached its conclusion. The distinction matters when reporting
# accuracy: a check that reads Razorpay's own `type` column cannot really be
# wrong, so scoring it alongside checks that infer something would overstate
# what the engine has demonstrated.
INFERRED: frozenset[ExceptionCode] = frozenset({
    ExceptionCode.FEE_MISMATCH,
    ExceptionCode.AMBIGUOUS_MATCH,
    ExceptionCode.SETTLEMENT_SHORTFALL,
    ExceptionCode.DUPLICATE_SETTLEMENT,
    ExceptionCode.SPLIT_CAPTURE,
    ExceptionCode.ORPHAN_PAYMENT,
    ExceptionCode.MISSING_RECEIPT,
    ExceptionCode.UNRESOLVED_NO_ROW,
})

LABELLED: frozenset[ExceptionCode] = frozenset({
    ExceptionCode.CHARGEBACK,
    ExceptionCode.FULL_REFUND,
    ExceptionCode.PARTIAL_REFUND,
})


@dataclass
class Match:
    """One reconciled pair, with the evidence that produced it."""
    order_id: str
    payment_id: str
    tier: Tier
    order_amount: float
    payment_amount: float
    fee: float
    tax: float
    net_to_bank: float
    settlement_utr: str
    rule: str                       # human-readable statement of what matched

    @property
    def confidence(self) -> float:
        return self.tier.confidence


@dataclass
class Exception_:
    """One thing the engine could not resolve, stated honestly.

    `possible_causes` exists because the engine frequently knows *that*
    something is wrong without being able to prove *why*. Listing the
    candidates is more useful — and more honest — than picking one.
    """
    code: ExceptionCode
    severity: str
    order_id: Optional[str]
    payment_id: Optional[str]
    amount_at_risk: float
    detail: str
    possible_causes: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        code: ExceptionCode,
        *,
        detail: str,
        amount_at_risk: float = 0.0,
        order_id: Optional[str] = None,
        payment_id: Optional[str] = None,
        possible_causes: Optional[list[str]] = None,
        evidence: Optional[dict] = None,
    ) -> "Exception_":
        return cls(
            code=code,
            severity=SEVERITY[code],
            order_id=order_id,
            payment_id=payment_id,
            amount_at_risk=round(amount_at_risk, 2),
            detail=detail,
            possible_causes=possible_causes or [],
            evidence=evidence or {},
        )


@dataclass
class ReconResult:
    """Everything one reconciliation run produced."""
    matches: list[Match] = field(default_factory=list)
    exceptions: list[Exception_] = field(default_factory=list)
    orders_seen: int = 0
    payments_seen: int = 0
    elapsed_seconds: float = 0.0

    @property
    def match_rate(self) -> float:
        if not self.orders_seen:
            return 0.0
        return len(self.matches) / self.orders_seen * 100.0

    @property
    def tier_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for m in self.matches:
            counts[m.tier.value] = counts.get(m.tier.value, 0) + 1
        return counts

    @property
    def exception_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.exceptions:
            counts[e.code.value] = counts.get(e.code.value, 0) + 1
        return counts

    @property
    def amount_at_risk(self) -> float:
        return round(sum(e.amount_at_risk for e in self.exceptions), 2)

    @property
    def throughput(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.orders_seen / self.elapsed_seconds
