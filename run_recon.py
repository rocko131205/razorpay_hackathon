"""Run one reconciliation cycle from the command line.

    python run_recon.py            reconcile whatever is in data/
    python run_recon.py --generate regenerate the three files first
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from recon.matcher import load_bank, load_orders, load_payments, reconcile
from recon.metrics import score
from recon.models import SEVERITY, ExceptionCode

DATA = Path("data")
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _rule(char: str = "─", width: int = 68) -> str:
    return char * width


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true", help="regenerate synthetic data first")
    ap.add_argument("--orders", type=int, default=250)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--show", type=int, default=6, help="exceptions to print in full")
    args = ap.parse_args()

    if args.generate or not (DATA / "orders.csv").exists():
        from recon.generator import generate
        generate(n_orders=args.orders, seed=args.seed, out_dir=DATA)

    truth = json.loads((DATA / "ground_truth.json").read_text(encoding="utf-8"))

    orders = load_orders(DATA / "orders.csv")
    payments = load_payments(DATA / "razorpay_recon.csv")
    bank = load_bank(DATA / "bank_statement.csv")

    result = reconcile(
        orders, payments, bank,
        settlement_utr=truth["settlement_utr"],
        fee_rate=truth["fee_rate"],
        gst_on_fee=truth["gst_on_fee"],
    )

    # ── Run summary ───────────────────────────────────────────────────────
    print()
    print(_rule("━"))
    print(f"  SETTLEMENT RECONCILIATION  ·  {truth['settlement_date']}  ·  {truth['settlement_utr']}")
    print(_rule("━"))
    print(f"  {result.orders_seen} orders  ·  {result.payments_seen} settled rows  ·  "
          f"{len(bank)} bank credits")
    print(f"  completed in {result.elapsed_seconds * 1000:.0f} ms  "
          f"({result.throughput:,.0f} orders/sec)")
    print()
    print(f"  MATCHED   {len(result.matches):>4} / {result.orders_seen}   "
          f"= {result.match_rate:.1f}%")
    for tier, n in sorted(result.tier_counts.items()):
        print(f"      {tier:<18} {n:>4}")
    print()
    print(f"  EXCEPTIONS {len(result.exceptions):>3}   "
          f"₹{result.amount_at_risk:,.2f} at risk")
    counts = result.exception_counts
    for code in sorted(counts, key=lambda c: (SEVERITY_ORDER[SEVERITY[ExceptionCode(c)]], c)):
        sev = SEVERITY[ExceptionCode(code)]
        print(f"      {sev:<9} {code:<22} {counts[code]:>4}")

    # ── Accuracy against the answer key ───────────────────────────────────
    s = score(result, truth)
    o = s["overall"]
    print()
    print(_rule())
    print("  ACCURACY vs GROUND TRUTH")
    print(_rule())
    print(f"  precision {o['precision']:.3f}   recall {o['recall']:.3f}   f1 {o['f1']:.3f}")
    print(f"  {o['tp']} correct  ·  {o['fp']} false alarms  ·  {o['fn']} missed")
    print()
    print(f"  {'exception type':<24}{'planted':>8}{'found':>7}{'prec':>7}{'recall':>8}")
    for code, m in sorted(s["per_type"].items()):
        print(f"  {code:<24}{m['planted']:>8}{m['raised']:>7}"
              f"{m['precision']:>7.2f}{m['recall']:>8.2f}")

    # ── The honest part: what it got wrong ────────────────────────────────
    misses = {c: m for c, m in s["per_type"].items() if m["fn"] or m["fp"]}
    if misses:
        print()
        print(_rule())
        print("  WHAT THIS ENGINE STILL GETS WRONG")
        print(_rule())
        for code, m in sorted(misses.items()):
            if m["fn"]:
                print(f"  {code}: missed {m['fn']} — e.g. {', '.join(m['missed'][:3])}")
            if m["fp"]:
                print(f"  {code}: {m['fp']} false alarms — e.g. {', '.join(str(x) for x in m['spurious'][:3])}")

    # ── A few exceptions in full ──────────────────────────────────────────
    print()
    print(_rule())
    print(f"  EXCEPTION QUEUE  ·  top {args.show} by severity then value")
    print(_rule())
    ranked = sorted(
        result.exceptions,
        key=lambda e: (SEVERITY_ORDER[e.severity], -e.amount_at_risk),
    )
    for e in ranked[:args.show]:
        ref = e.order_id or e.payment_id or "—"
        print(f"\n  [{e.severity}] {e.code.value}  ·  {ref}  ·  ₹{e.amount_at_risk:,.2f} at risk")
        print(f"      {e.detail}")
        for cause in e.possible_causes:
            print(f"        · {cause}")
    print()


if __name__ == "__main__":
    main()
