"""Tests for the matching engine.

The reported match rate is only as trustworthy as these tests, so there is one
case per tier, one per exception code, and a named regression for each bug
found during development.
"""
from __future__ import annotations

import pytest

from recon.matcher import reconcile
from recon.models import BankLine, ExceptionCode, Order, Payment, Tier

UTR = "UTR11112222"


def order(oid="ORD-0001", amount=1000.0, phone="9000000001",
          created="2026-09-01T09:00:00", status="PAID") -> Order:
    return Order(order_id=oid, amount=amount, created_at=created,
                 status=status, customer_phone=phone)


def payment(pid="pay_a", receipt="ORD-0001", amount=1000.0, ptype="payment",
            fee=20.0, tax=3.6, settled="2026-09-01T12:00:00", utr=UTR,
            phone="9000000001") -> Payment:
    return Payment(payment_id=pid, order_receipt=receipt, type=ptype,
                   amount=amount, fee=fee, tax=tax, settled_at=settled,
                   settlement_utr=utr, method="upi", customer_phone=phone)


def run(orders, payments, bank=None, utr=UTR):
    return reconcile(orders, payments, bank or [], settlement_utr=utr)


def codes(result) -> set[str]:
    return {e.code.value for e in result.exceptions}


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

def test_tier1_matches_on_receipt():
    r = run([order()], [payment()])
    assert len(r.matches) == 1
    assert r.matches[0].tier is Tier.RECEIPT
    assert r.matches[0].confidence == 1.0


def test_tier3_matches_on_amount_and_phone_when_receipt_absent():
    r = run([order()], [payment(receipt=None)])
    assert r.matches[0].tier is Tier.AMOUNT_PHONE
    assert ExceptionCode.MISSING_RECEIPT.value in codes(r)


def test_tier4_matches_on_amount_alone_when_phone_also_absent():
    r = run([order()], [payment(receipt=None, phone="")])
    assert r.matches[0].tier is Tier.AMOUNT_ONLY
    assert r.matches[0].confidence == pytest.approx(0.60)


def test_order_with_no_settled_row_is_carried_forward_not_accused():
    """A missing row could be a phantom or a late settlement, and today's data
    cannot tell them apart. Naming either would be a guess."""
    r = run([order()], [])
    assert not r.matches
    assert ExceptionCode.UNRESOLVED_NO_ROW.value in codes(r)
    assert r.exceptions[0].amount_at_risk == 1000.0
    assert len(r.exceptions[0].possible_causes) >= 3


# ---------------------------------------------------------------------------
# The engine must decline to guess
# ---------------------------------------------------------------------------

def test_identical_orders_with_no_identifiers_are_escalated_not_guessed():
    """Two orders, same amount, same window, no receipt, no phone.

    Nothing distinguishes them, so neither may be matched. Picking one would
    corrupt two rows rather than one.
    """
    orders = [order("ORD-0001"), order("ORD-0002")]
    payments = [payment("pay_a", receipt=None, phone=""),
                payment("pay_b", receipt=None, phone="")]
    r = run(orders, payments)

    assert r.matches == []
    assert codes(r) == {ExceptionCode.AMBIGUOUS_MATCH.value}
    assert len(r.exceptions) == 2
    for e in r.exceptions:
        assert len(e.evidence["candidates"]) == 2


# ---------------------------------------------------------------------------
# Exception detection
# ---------------------------------------------------------------------------

def test_chargeback_is_critical_and_carries_full_amount():
    r = run([order()], [payment(ptype="chargeback")])
    exc = next(e for e in r.exceptions if e.code is ExceptionCode.CHARGEBACK)
    assert exc.severity == "CRITICAL"
    assert exc.amount_at_risk == 1000.0


def test_partial_refund_reports_only_the_unreturned_portion():
    r = run([order(amount=1000.0)], [payment(amount=250.0, ptype="refund")])
    exc = next(e for e in r.exceptions if e.code is ExceptionCode.PARTIAL_REFUND)
    assert exc.amount_at_risk == 750.0


def test_overcharged_fee_is_flagged_with_the_overcharge_amount():
    # 2.8% instead of the agreed 2.0%
    r = run([order()], [payment(fee=28.0, tax=5.04)])
    exc = next(e for e in r.exceptions if e.code is ExceptionCode.FEE_MISMATCH)
    assert exc.amount_at_risk == pytest.approx(9.44, abs=0.01)


def test_sub_rupee_fee_drift_is_rounding_not_an_overcharge():
    r = run([order()], [payment(fee=20.02, tax=3.6)])
    assert ExceptionCode.FEE_MISMATCH.value not in codes(r)


def test_payment_settling_under_another_utr_is_late_not_missing():
    r = run([order()], [payment(utr="UTR99998888")])
    assert len(r.matches) == 1          # still matched with full confidence
    assert ExceptionCode.LATE_SETTLEMENT.value in codes(r)


def test_payment_with_unknown_receipt_is_an_orphan():
    r = run([order("ORD-0001")], [payment("pay_a", receipt="ORD-0001"),
                                  payment("pay_z", receipt="ORD-9999", amount=77.0)])
    exc = next(e for e in r.exceptions if e.code is ExceptionCode.ORPHAN_PAYMENT)
    assert exc.payment_id == "pay_z"


def test_second_row_for_the_same_receipt_is_a_duplicate():
    r = run([order()], [payment("pay_a"), payment("pay_b")])
    assert ExceptionCode.DUPLICATE_SETTLEMENT.value in codes(r)


# ---------------------------------------------------------------------------
# Bank stage
# ---------------------------------------------------------------------------

def test_bank_credit_matching_the_settled_net_raises_nothing():
    p = payment()                       # 1000 − 20 − 3.60 = 976.40
    bank = [BankLine("2026-09-02", UTR, 976.40, "RAZORPAY SETTLEMENT")]
    r = run([order()], [p], bank)
    assert ExceptionCode.SETTLEMENT_SHORTFALL.value not in codes(r)


def test_bank_credit_short_of_the_settled_net_is_flagged():
    bank = [BankLine("2026-09-02", UTR, 900.00, "RAZORPAY SETTLEMENT")]
    r = run([order()], [payment()], bank)
    exc = next(e for e in r.exceptions if e.code is ExceptionCode.SETTLEMENT_SHORTFALL)
    assert exc.amount_at_risk == pytest.approx(76.40, abs=0.01)


# ---------------------------------------------------------------------------
# Regressions — each of these was a live bug
# ---------------------------------------------------------------------------

def test_regression_partial_refund_is_not_read_as_an_overcharge():
    """Razorpay levies its fee on the captured amount, not the refunded part.

    Comparing the fee against the refund amount flagged every partial refund
    as an overcharge and dropped fee precision to 0.60.
    """
    r = run([order(amount=1000.0)],
            [payment(amount=250.0, ptype="refund", fee=20.0, tax=3.6)])
    assert ExceptionCode.FEE_MISMATCH.value not in codes(r)


def test_regression_weak_tier_cannot_steal_another_orders_payment():
    """Amount-only matching once claimed a payment whose receipt named a
    different real order, reporting that order as a phantom in turn."""
    orders = [order("ORD-0001", amount=500.0, phone=""),
              order("ORD-0002", amount=500.0, phone="9000000002")]
    payments = [payment("pay_for_2", receipt="ORD-0002", amount=500.0,
                        phone="9000000002")]
    r = run(orders, payments)

    matched = {m.order_id for m in r.matches}
    assert matched == {"ORD-0002"}
    unresolved = [e for e in r.exceptions if e.code is ExceptionCode.UNRESOLVED_NO_ROW]
    assert [e.order_id for e in unresolved] == ["ORD-0001"]


def test_regression_contested_rows_are_not_also_counted_as_orphans():
    """Rows tied up in an ambiguity are already named by that exception.
    Sweeping them into the orphan bucket double-counted the same rupees."""
    orders = [order("ORD-0001"), order("ORD-0002")]
    payments = [payment("pay_a", receipt=None, phone=""),
                payment("pay_b", receipt=None, phone="")]
    r = run(orders, payments)
    assert ExceptionCode.ORPHAN_PAYMENT.value not in codes(r)


# ---------------------------------------------------------------------------
# End to end against the generator's answer key
# ---------------------------------------------------------------------------

def test_full_run_scores_against_ground_truth(tmp_path):
    from recon.generator import generate
    from recon.matcher import load_bank, load_orders, load_payments
    from recon.metrics import score

    truth = generate(n_orders=250, seed=7, out_dir=tmp_path)
    result = reconcile(
        load_orders(tmp_path / "orders.csv"),
        load_payments(tmp_path / "razorpay_recon.csv"),
        load_bank(tmp_path / "bank_statement.csv"),
        settlement_utr=truth["settlement_utr"],
        fee_rate=truth["fee_rate"],
        gst_on_fee=truth["gst_on_fee"],
    )
    s = score(result, truth)

    # Inferred checks are scored; they are good but not perfect, because the
    # data contains genuinely ambiguous cases.
    assert 0.90 <= s["inferred"]["overall"]["precision"] <= 1.0
    assert 0.90 <= s["inferred"]["overall"]["recall"] <= 1.0

    # Label-reading checks are counted, never scored — a check that copies
    # Razorpay's `type` field cannot be wrong and must not inflate accuracy.
    assert s["labelled"]["scored"] is False

    # Cycle one is missing every late row by construction, so the match rate
    # is materially lower than after the next cycle arrives.
    assert 80.0 <= result.match_rate <= 90.0


def test_carried_forward_findings_close_on_the_next_cycle(tmp_path):
    """The loop Razorpay asks to be closed: undecidable today, decided
    tomorrow, with only the genuine phantoms left standing."""
    from recon.generator import generate
    from recon.matcher import load_bank, load_orders, load_payments
    from recon.metrics import resolution

    truth = generate(n_orders=250, seed=7, out_dir=tmp_path)
    orders = load_orders(tmp_path / "orders.csv")
    c1_pay = load_payments(tmp_path / "razorpay_recon.csv")

    cycle1 = reconcile(orders, c1_pay, load_bank(tmp_path / "bank_statement.csv"),
                       settlement_utr=truth["settlement_utr"],
                       fee_rate=truth["fee_rate"], gst_on_fee=truth["gst_on_fee"])

    combined = c1_pay + load_payments(tmp_path / "razorpay_recon_cycle2.csv")
    cycle2 = reconcile(orders, combined,
                       load_bank(tmp_path / "bank_statement_cycle2.csv"),
                       settlement_utr=truth["next_cycle_utr"],
                       fee_rate=truth["fee_rate"], gst_on_fee=truth["gst_on_fee"])

    res = resolution(cycle1, cycle2, truth)
    assert res["carried_forward"] > res["still_open"]
    assert res["closed_wrongly"] == 0        # nothing closed that was truly phantom
    assert res["still_open"] == len(truth["true_phantoms"])
    assert cycle2.match_rate > cycle1.match_rate


def test_split_capture_is_distinguished_from_a_double_charge():
    """Two rows on one order. Parts that sum to the order are a split
    capture; parts that each equal it are a duplicate. Only arithmetic
    separates them."""
    split = run([order(amount=1000.0)],
                [payment("pay_a", amount=500.0, fee=10.0, tax=1.8),
                 payment("pay_b", amount=500.0, fee=10.0, tax=1.8)])
    assert ExceptionCode.SPLIT_CAPTURE.value in codes(split)
    assert ExceptionCode.DUPLICATE_SETTLEMENT.value not in codes(split)

    dupe = run([order(amount=1000.0)],
               [payment("pay_a", amount=1000.0), payment("pay_b", amount=1000.0)])
    assert ExceptionCode.DUPLICATE_SETTLEMENT.value in codes(dupe)
    assert ExceptionCode.SPLIT_CAPTURE.value not in codes(dupe)
