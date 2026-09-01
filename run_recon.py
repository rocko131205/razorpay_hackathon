"""Run one reconciliation cycle from the command line.

    python run_recon.py            reconcile whatever is in data/
    python run_recon.py --generate regenerate the three files first
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from recon.matcher import load_bank, load_orders, load_payments, reconcile
from recon.metrics import resolution, score
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

    # ── Accuracy, split by how each conclusion was reached ───────────────
    s = score(result, truth)
    print()
    print(_rule())
    print("  ACCURACY")
    print(_rule())

    m = s["matching"]
    print(f"  MATCHING — the hard part")
    print(f"      match rate {m['match_rate']:.1f}%   "
          f"({m['matched']}/{m['orders']}, {m['unmatched']} escalated)")

    inf = s["inferred"]["overall"]
    print()
    print(f"  INFERRED — the engine worked these out")
    print(f"      precision {inf['precision']:.3f}   recall {inf['recall']:.3f}   "
          f"({inf['fp']} false alarms, {inf['fn']} missed)")
    for code, x in sorted(s["inferred"]["per_type"].items()):
        print(f"      {code:<24}{x['planted']:>5} planted{x['raised']:>6} found"
              f"{x['precision']:>7.2f}{x['recall']:>7.2f}")

    print()
    print(f"  READ FROM RAZORPAY'S `type` COLUMN — reported, not inferred")
    for code, x in sorted(s["labelled"]["per_type"].items()):
        print(f"      {code:<24}{x['raised']:>5} of {x['planted']} reported as labelled")
    print("      (not scored: a check that copies a field cannot be wrong)")

    # ── The loop: run the next cycle and see what closes itself ──────────
    nxt = DATA / "razorpay_recon_cycle2.csv"
    if nxt.exists():
        combined = load_payments(DATA / "razorpay_recon.csv") + load_payments(nxt)
        bank2_path = DATA / "bank_statement_cycle2.csv"
        bank2 = load_bank(bank2_path if bank2_path.exists() else DATA / "bank_statement.csv")
        cycle2 = reconcile(
            load_orders(DATA / "orders.csv"), combined, bank2,
            settlement_utr=truth["next_cycle_utr"],
            fee_rate=truth["fee_rate"], gst_on_fee=truth["gst_on_fee"],
        )
        res = resolution(result, cycle2, truth)
        print()
        print(_rule())
        print("  CLOSING THE LOOP — running the next cycle")
        print(_rule())
        print(f"  carried forward from cycle 1   {res['carried_forward']:>4}")
        print(f"  closed once the rows arrived   {res['closed_next_cycle']:>4}"
              f"   ({res['closed_correctly']} correctly, {res['closed_wrongly']} wrongly)")
        print(f"  still open after cycle 2       {res['still_open']:>4}"
              f"   ({res['remaining_are_real_phantoms']} are genuine phantoms)")
        print(f"  cycle 2 match rate             {cycle2.match_rate:>7.1f}%")

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
