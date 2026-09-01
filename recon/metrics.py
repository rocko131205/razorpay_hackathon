"""Scores a reconciliation run against the generator's answer key.

This module is the reason the project can state an accuracy figure instead of
asserting one. Because generator.py records every defect it planted, each
exception the engine raises can be classified as a true positive, false
positive, or false negative — per defect type, not just in aggregate.

Reported honestly means reporting the misses too.
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import ReconResult


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


def score(result: ReconResult, ground_truth: dict | str | Path) -> dict:
    """Compare raised exceptions against planted defects.

    Matching is done on (defect type, order id) so a right answer for the
    wrong row does not count as correct.
    """
    if not isinstance(ground_truth, dict):
        ground_truth = json.loads(Path(ground_truth).read_text(encoding="utf-8"))

    # Planted defects, keyed by type -> set of order ids (or payment ids for
    # orphans, which have no order at all).
    planted: dict[str, set[str]] = {}
    for d in ground_truth["defects"]:
        key = d.get("order_id") or d.get("payment_id")
        planted.setdefault(d["defect"], set()).add(key)

    raised: dict[str, set[str]] = {}
    for e in result.exceptions:
        key = e.order_id or e.payment_id
        raised.setdefault(e.code.value, set()).add(key)

    per_type: dict[str, dict] = {}
    tp_total = fp_total = fn_total = 0

    for code in sorted(set(planted) | set(raised)):
        want = planted.get(code, set())
        got = raised.get(code, set())
        tp = len(want & got)
        fp = len(got - want)
        fn = len(want - got)
        p, r, f1 = _prf(tp, fp, fn)
        per_type[code] = {
            "planted": len(want), "raised": len(got),
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3),
            "missed": sorted(want - got)[:5],
            "spurious": sorted(got - want)[:5],
        }
        tp_total += tp
        fp_total += fp
        fn_total += fn

    p, r, f1 = _prf(tp_total, fp_total, fn_total)
    return {
        "overall": {
            "tp": tp_total, "fp": fp_total, "fn": fn_total,
            "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3),
        },
        "per_type": per_type,
    }
