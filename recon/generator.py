"""Synthetic three-file generator with a known answer key.

Produces the three files a merchant actually reconciles — their own order
list, Razorpay's settlement recon report, and a bank statement — and writes
a ground_truth.json recording every defect it deliberately introduced.

The answer key is the point. Without it the engine can only claim accuracy;
with it, accuracy can be measured. Every defect below is injected at a known
order id, so metrics.py can score the matcher honestly.

Shape of the report mirrors Razorpay's settlement recon combined report:
payment_id, order_receipt, type, amount, fee, tax, settled_at,
settlement_utr, method.
"""
from __future__ import annotations

import csv
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from .models import ExceptionCode

# Razorpay's standard rate for the merchant, and tax on the fee.
FEE_RATE = 0.02
GST_ON_FEE = 0.18

# How many of each defect to plant in a 250-order run. Tuned so every code
# is exercised while the bulk of rows stay clean, as in real data.
DEFECT_MIX: dict[ExceptionCode, int] = {
    ExceptionCode.LATE_SETTLEMENT:      22,
    ExceptionCode.FEE_MISMATCH:         12,
    ExceptionCode.PARTIAL_REFUND:        8,
    ExceptionCode.FULL_REFUND:           6,
    ExceptionCode.CHARGEBACK:            4,
    ExceptionCode.MISSING_RECEIPT:      10,
    ExceptionCode.DUPLICATE_SETTLEMENT:  3,
    ExceptionCode.ORPHAN_PAYMENT:        4,
    ExceptionCode.PHANTOM_ORDER:         5,
}


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _fee_for(amount: float, rate: float = FEE_RATE) -> tuple[float, float]:
    fee = _money(amount * rate)
    tax = _money(fee * GST_ON_FEE)
    return fee, tax


def generate(
    n_orders: int = 250,
    seed: int = 7,
    out_dir: str | Path = "data",
    settlement_date: str = "2026-09-01",
) -> dict:
    """Generate the three files plus the answer key.

    Returns the ground-truth dict (also written to disk).
    """
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    base = datetime.fromisoformat(settlement_date)
    utr_today = f"UTR{rng.randint(10_000_000, 99_999_999)}"
    utr_next = f"UTR{rng.randint(10_000_000, 99_999_999)}"
    settlement_id = f"setl_{rng.randrange(16**14):014x}"

    # ── Decide which orders carry which defect ────────────────────────────
    # Orders are numbered ORD-0001..ORD-{n}. PHANTOM and ORPHAN are special:
    # phantom orders exist only in the shop's file, orphan payments only in
    # Razorpay's, so they are allocated outside the shared pool.
    ids = [f"ORD-{i:04d}" for i in range(1, n_orders + 1)]
    pool = ids[:]
    rng.shuffle(pool)

    assigned: dict[str, ExceptionCode] = {}
    cursor = 0
    for code, count in DEFECT_MIX.items():
        if code is ExceptionCode.ORPHAN_PAYMENT:
            continue  # orphans have no order at all — handled separately
        for _ in range(count):
            if cursor >= len(pool):
                break
            assigned[pool[cursor]] = code
            cursor += 1

    orders: list[dict] = []
    payments: list[dict] = []
    truth: list[dict] = []

    for oid in ids:
        amount = _money(rng.choice([199, 299, 499, 750, 999, 1250, 1999, 2500, 3499, 4999])
                        + rng.randint(0, 99))
        phone = f"9{rng.randint(100000000, 999999999)}"
        created = (base - timedelta(hours=rng.randint(2, 20))).isoformat(timespec="seconds")
        defect = assigned.get(oid)

        # ---- the shop's own order row -----------------------------------
        order_status = "PAID"
        if defect is ExceptionCode.FULL_REFUND:
            order_status = "REFUNDED"
        orders.append({
            "order_id": oid,
            "amount": amount,
            "created_at": created,
            "status": order_status,
            "customer_phone": phone,
        })

        # PHANTOM_ORDER: the shop believes it was paid, Razorpay has no record.
        # This is the expensive one — goods shipped, no money.
        if defect is ExceptionCode.PHANTOM_ORDER:
            truth.append({"order_id": oid, "defect": defect.value, "amount_at_risk": amount})
            continue

        pid = f"pay_{rng.randrange(16**14):014x}"
        fee, tax = _fee_for(amount)
        settled_at = base.isoformat(timespec="seconds")
        utr = utr_today
        receipt: str | None = oid
        ptype = "payment"
        pay_amount = amount

        if defect is ExceptionCode.LATE_SETTLEMENT:
            # Paid, but lands in the next settlement cycle. Resolves itself
            # on tomorrow's run — the canonical self-resolving exception.
            settled_at = (base + timedelta(days=1)).isoformat(timespec="seconds")
            utr = utr_next

        elif defect is ExceptionCode.FEE_MISMATCH:
            # Charged at a higher rate than agreed. Silent money leak.
            bad_rate = FEE_RATE + rng.choice([0.004, 0.006, 0.008])
            fee, tax = _fee_for(amount, bad_rate)

        elif defect is ExceptionCode.MISSING_RECEIPT:
            # Merchant never set `receipt` when creating the order, so the
            # primary join key is gone and the matcher must fall back.
            receipt = None

        elif defect is ExceptionCode.PARTIAL_REFUND:
            pay_amount = _money(amount * rng.choice([0.25, 0.4, 0.5]))
            ptype = "refund"

        elif defect is ExceptionCode.FULL_REFUND:
            ptype = "refund"

        elif defect is ExceptionCode.CHARGEBACK:
            ptype = "chargeback"

        payments.append({
            "payment_id": pid,
            "order_receipt": receipt or "",
            "type": ptype,
            "amount": pay_amount,
            "fee": fee,
            "tax": tax,
            "settled_at": settled_at,
            "settlement_utr": utr,
            "method": rng.choice(["upi", "card", "netbanking", "wallet"]),
            "customer_phone": phone,
        })

        if defect is ExceptionCode.DUPLICATE_SETTLEMENT:
            # Same payment appears twice in the report — double-counted money.
            dup = dict(payments[-1])
            dup["payment_id"] = f"pay_{rng.randrange(16**14):014x}"
            payments.append(dup)

        if defect is not None:
            at_risk = {
                ExceptionCode.FEE_MISMATCH: fee - _fee_for(amount)[0],
                ExceptionCode.PARTIAL_REFUND: amount - pay_amount,
                ExceptionCode.FULL_REFUND: amount,
                ExceptionCode.CHARGEBACK: amount,
                ExceptionCode.DUPLICATE_SETTLEMENT: amount,
                ExceptionCode.LATE_SETTLEMENT: amount,
            }.get(defect, 0.0)
            truth.append({"order_id": oid, "defect": defect.value,
                          "amount_at_risk": _money(at_risk)})

    # ── Orphan payments: present in Razorpay, absent from the shop ────────
    for _ in range(DEFECT_MIX[ExceptionCode.ORPHAN_PAYMENT]):
        amount = _money(rng.randint(200, 4000))
        fee, tax = _fee_for(amount)
        pid = f"pay_{rng.randrange(16**14):014x}"
        payments.append({
            "payment_id": pid,
            "order_receipt": f"ORD-{rng.randint(9000, 9999)}",  # id the shop never issued
            "type": "payment",
            "amount": amount,
            "fee": fee,
            "tax": tax,
            "settled_at": base.isoformat(timespec="seconds"),
            "settlement_utr": utr_today,
            "method": rng.choice(["upi", "card"]),
            "customer_phone": f"9{rng.randint(100000000, 999999999)}",
        })
        truth.append({"order_id": None, "payment_id": pid,
                      "defect": ExceptionCode.ORPHAN_PAYMENT.value,
                      "amount_at_risk": amount})

    rng.shuffle(payments)

    # ── Bank statement: one credit per settlement, net of everything ──────
    def _net(rows: list[dict]) -> float:
        total = 0.0
        for r in rows:
            gross = r["amount"] if r["type"] == "payment" else -r["amount"]
            total += gross - r["fee"] - r["tax"]
        return _money(total)

    today_rows = [p for p in payments if p["settlement_utr"] == utr_today]
    next_rows = [p for p in payments if p["settlement_utr"] == utr_next]

    bank = [{
        "date": (base + timedelta(days=1)).date().isoformat(),
        "utr": utr_today,
        "credit": _net(today_rows),
        "description": f"RAZORPAY SETTLEMENT {settlement_id}",
    }]
    if next_rows:
        bank.append({
            "date": (base + timedelta(days=2)).date().isoformat(),
            "utr": utr_next,
            "credit": _net(next_rows),
            "description": "RAZORPAY SETTLEMENT (next cycle)",
        })

    # ── Write ─────────────────────────────────────────────────────────────
    def _write(name: str, rows: list[dict]) -> None:
        path = out / name
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    _write("orders.csv", orders)
    _write("razorpay_recon.csv", payments)
    _write("bank_statement.csv", bank)

    ground_truth = {
        "settlement_date": settlement_date,
        "settlement_utr": utr_today,
        "next_cycle_utr": utr_next,
        "n_orders": len(orders),
        "n_payments": len(payments),
        "fee_rate": FEE_RATE,
        "gst_on_fee": GST_ON_FEE,
        "defects": truth,
        "defect_counts": {c.value: n for c, n in DEFECT_MIX.items()},
    }
    (out / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2) + "\n", encoding="utf-8"
    )
    return ground_truth


if __name__ == "__main__":
    gt = generate()
    print(f"orders          {gt['n_orders']}")
    print(f"payments        {gt['n_payments']}")
    print(f"defects planted {len(gt['defects'])}")
    for code, n in gt["defect_counts"].items():
        print(f"  {code:<22} {n}")
