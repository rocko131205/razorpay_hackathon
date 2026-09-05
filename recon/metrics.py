"""Scores a reconciliation run against the generator's answer key.

Three things are reported separately, because blending them would overstate
what the engine has actually demonstrated:

  matching     pairing orders to settled rows under missing and colliding
               identifiers. The genuinely hard part.

  inferred     checks that work something out — a fee against a threshold with
               rounding noise present, a split capture against a double charge,
               a bank credit against the net of its rows.

  labelled     checks that report Razorpay's own `type` column. These cannot
               really be wrong, so they are counted but not scored. Presenting
               them as accuracy would be claiming credit for reading a field.

Across cycles, `resolution` reports the loop: how many findings were carried
forward, and how many closed on their own when the next cycle's rows arrived.
"""
from __future__ import annotations

import json
from pathlib import Path

from recon.models import INFERRED, LABELLED, ExceptionCode, ReconResult


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


def _load(ground_truth: dict | str | Path) -> dict:
    if isinstance(ground_truth, dict):
        return ground_truth
    return json.loads(Path(ground_truth).read_text(encoding="utf-8"))


def _index(result: ReconResult, truth: dict) -> tuple[dict, dict]:
    """Planted defects and raised exceptions, keyed by code -> set of ids."""
    planted: dict[str, set] = {}
    for d in truth["defects"]:
        key = d.get("order_id") or d.get("payment_id")
        planted.setdefault(d["defect"], set()).add(key)

    raised: dict[str, set] = {}
    for e in result.exceptions:
        key = e.order_id or e.payment_id
        raised.setdefault(e.code.value, set()).add(key)
    return planted, raised


def score(result: ReconResult, ground_truth: dict | str | Path,
          cycle: int = 1) -> dict:
    truth = _load(ground_truth)
    planted, raised = _index(result, truth)

    # LATE_SETTLEMENT rows are absent from cycle one by construction, so the
    # engine reports them as UNRESOLVED_NO_ROW. That is the correct answer for
    # this cycle, not a miss, so the expectation is remapped rather than the
    # engine being penalised for declining to guess.
    #
    # By cycle two those rows have arrived and should have been matched, so only
    # the genuine phantoms are still expected to be open. Scoring cycle two
    # against cycle one's expectation would count every correct resolution as a
    # miss — punishing the engine for closing the loop.
    pending = set(truth.get("true_phantoms", []))
    if cycle == 1:
        pending |= set(truth.get("resolves_on_cycle2", []))
    expected_unresolved = {c: v for c, v in planted.items()
                           if c not in {"LATE_SETTLEMENT", "PHANTOM_ORDER"}}
    if pending:
        expected_unresolved[ExceptionCode.UNRESOLVED_NO_ROW.value] = pending
    planted = expected_unresolved

    sections: dict[str, dict] = {}
    for name, codes in (("inferred", INFERRED), ("labelled", LABELLED)):
        names = {c.value for c in codes}
        per_type: dict[str, dict] = {}
        tp_t = fp_t = fn_t = 0
        for code in sorted((set(planted) | set(raised)) & names):
            want, got = planted.get(code, set()), raised.get(code, set())
            tp, fp, fn = len(want & got), len(got - want), len(want - got)
            p, r, f1 = _prf(tp, fp, fn)
            per_type[code] = {
                "planted": len(want), "raised": len(got),
                "tp": tp, "fp": fp, "fn": fn,
                "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3),
                "missed": sorted(str(x) for x in (want - got))[:5],
                "spurious": sorted(str(x) for x in (got - want))[:5],
            }
            tp_t += tp
            fp_t += fp
            fn_t += fn
        p, r, f1 = _prf(tp_t, fp_t, fn_t)
        sections[name] = {
            "per_type": per_type,
            "overall": {"tp": tp_t, "fp": fp_t, "fn": fn_t,
                        "precision": round(p, 3), "recall": round(r, 3),
                        "f1": round(f1, 3)},
            "scored": name == "inferred",
        }

    sections["matching"] = {
        "match_rate": round(result.match_rate, 2),
        "matched": len(result.matches),
        "orders": result.orders_seen,
        "unmatched": result.orders_seen - len(result.matches),
        "tiers": result.tier_counts,
    }
    return sections


def resolution(cycle1: ReconResult, cycle2: ReconResult, ground_truth) -> dict:
    """How the carried-forward findings fared once the next cycle arrived.

    This is the loop Razorpay's brief asks to be closed: a finding that could
    not be decided today is not a failure, provided it is tracked and either
    closes or hardens into a real problem tomorrow.
    """
    truth = _load(ground_truth)

    def open_ids(r: ReconResult) -> set[str]:
        return {e.order_id for e in r.exceptions
                if e.code is ExceptionCode.UNRESOLVED_NO_ROW and e.order_id}

    before, after = open_ids(cycle1), open_ids(cycle2)
    closed = before - after
    expected_close = set(truth.get("resolves_on_cycle2", []))
    real_phantoms = set(truth.get("true_phantoms", []))

    return {
        "carried_forward": len(before),
        "closed_next_cycle": len(closed),
        "still_open": len(after),
        "closed_correctly": len(closed & expected_close),
        "closed_wrongly": len(closed & real_phantoms),
        "remaining_are_real_phantoms": len(after & real_phantoms),
        "remaining_unexpected": sorted(after - real_phantoms)[:5],
    }
