"""Settlement Q&A — answers questions about a completed reconciliation.

The controller's real question is never "what is a chargeback". It is "why is
Thursday's payout ₹4,312 short". Answering that means reading the match ledger,
which is exactly what this does — and nothing else.

Two rules make the answers trustworthy:

  1. Retrieval is deterministic. Python selects the relevant ledger rows before
     the model is called, so the model never searches and never has to be
     trusted to find the right record.

  2. The model may not do arithmetic. Every figure it can quote is pre-computed
     and handed to it. If a number is not in the context, the correct answer is
     that the data does not show it.

This is the same division the matching engine keeps: Python decides, the model
explains. It is what allows the reported match rate to mean anything.

The provider is interchangeable, because none of the above depends on which
model answers. Anthropic and Gemini are both supported; whichever key is
present is used. Without either, the module degrades to a deterministic summary
rather than failing — the dashboard must remain demonstrable with no network.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from .config import ensure_loaded
from .models import ExceptionCode, ReconResult

ANTHROPIC_MODEL = "claude-opus-5"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"
MAX_ROWS = 40

SYSTEM = """You answer questions about a completed settlement reconciliation for \
a merchant's finance controller.

You are given facts that a deterministic engine already computed: matched pairs, \
exceptions, and settlement totals. Your job is to explain them clearly.

Rules you must follow:
- Never calculate. Every figure you cite must appear verbatim in the facts.
- Cite the order or payment id behind each claim, e.g. (ORD-0190).
- If the facts do not answer the question, say so plainly and name what is \
missing. Do not speculate about causes the facts do not support.
- An exception listing several possible causes has not been diagnosed. Present \
those as open possibilities, never as the established reason.
- Be brief. A controller wants the answer, then the evidence."""


@dataclass
class Answer:
    text: str
    rows_used: int
    grounded: bool          # False when produced without the model
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# Deterministic retrieval — Python decides what the model is allowed to see
# ---------------------------------------------------------------------------

_ID = re.compile(r"\b(ORD-\d{3,6}|pay_[0-9a-f]{6,}|UTR\d{5,})\b", re.IGNORECASE)


def _mentioned_ids(question: str) -> set[str]:
    return {m.group(0).upper() if m.group(0).upper().startswith(("ORD", "UTR"))
            else m.group(0) for m in _ID.finditer(question)}


def select_context(result: ReconResult, question: str) -> tuple[list[str], int]:
    """Pick the ledger rows that bear on the question.

    Explicit ids win. Otherwise the largest exceptions by money at risk are
    used, since that is what a shortfall question is nearly always about.
    """
    lines: list[str] = []
    wanted = _mentioned_ids(question)

    lines.append(
        f"RUN: {len(result.matches)} of {result.orders_seen} orders matched "
        f"({result.match_rate:.1f}%). {len(result.exceptions)} exceptions, "
        f"₹{result.amount_at_risk:,.2f} at risk."
    )
    tiers = ", ".join(f"{t}={n}" for t, n in sorted(result.tier_counts.items()))
    lines.append(f"MATCH TIERS: {tiers}")

    counts = result.exception_counts
    lines.append("EXCEPTION COUNTS: " + ", ".join(
        f"{code}={n}" for code, n in sorted(counts.items())))

    if wanted:
        for m in result.matches:
            if m.order_id in wanted or m.payment_id in wanted or m.settlement_utr in wanted:
                lines.append(
                    f"MATCH {m.order_id} <- {m.payment_id} via {m.tier.value} "
                    f"({m.confidence:.0%} confidence, rule: {m.rule}); "
                    f"order ₹{m.order_amount:,.2f}, settled ₹{m.payment_amount:,.2f}, "
                    f"fee ₹{m.fee:,.2f}, tax ₹{m.tax:,.2f}, "
                    f"net to bank ₹{m.net_to_bank:,.2f}, UTR {m.settlement_utr}"
                )

    pool = [e for e in result.exceptions
            if not wanted or (e.order_id in wanted or e.payment_id in wanted)]
    pool.sort(key=lambda e: -e.amount_at_risk)

    for e in pool[:MAX_ROWS]:
        ref = e.order_id or e.payment_id or "—"
        line = (f"EXCEPTION {e.code.value} [{e.severity}] {ref} "
                f"₹{e.amount_at_risk:,.2f} at risk — {e.detail}")
        if e.possible_causes:
            line += " POSSIBLE CAUSES (undiagnosed): " + "; ".join(e.possible_causes)
        lines.append(line)

    return lines, len(pool[:MAX_ROWS])


# ---------------------------------------------------------------------------
# Fallback — a real answer without a model
# ---------------------------------------------------------------------------

def _deterministic_answer(result: ReconResult, question: str,
                          context: list[str]) -> Answer:
    wanted = _mentioned_ids(question)
    hits = [e for e in result.exceptions
            if e.order_id in wanted or e.payment_id in wanted]

    if hits:
        e = hits[0]
        body = (f"{e.order_id or e.payment_id}: {e.code.value} ({e.severity}). "
                f"{e.detail}")
        if e.possible_causes:
            body += " Possible causes, none confirmed: " + "; ".join(e.possible_causes)
    else:
        top = sorted(result.exceptions, key=lambda x: -x.amount_at_risk)[:3]
        body = (
            f"{result.match_rate:.1f}% of orders matched. "
            f"₹{result.amount_at_risk:,.2f} sits across {len(result.exceptions)} "
            f"exceptions. Largest: "
            + "; ".join(f"{e.code.value} {e.order_id or e.payment_id} "
                        f"₹{e.amount_at_risk:,.2f}" for e in top)
            + "."
        )
    return Answer(
        text=body + "\n\n(Answered from the ledger directly — no model key configured.)",
        rows_used=len(context),
        grounded=False,
    )


# ---------------------------------------------------------------------------
# Ask
# ---------------------------------------------------------------------------

def provider() -> Optional[str]:
    """Which model backend is configured, if any.

    Anthropic wins when both are present, for no reason beyond needing a
    deterministic answer when asked twice.
    """
    ensure_loaded()
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"
    if os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip():
        return "gemini"
    return None


def available() -> bool:
    return provider() is not None


def _gemini_key() -> str:
    return (os.getenv("GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_API_KEY", "").strip())


def _gemini_model() -> str:
    """Resolve a model that this key can actually call.

    Model names move around, and a stale default 404s. Asking the API which
    models support generateContent is cheaper than being wrong in a demo.
    """
    import requests
    preferred = os.getenv("GEMINI_MODEL", "").strip()
    if preferred:
        return preferred
    try:
        resp = requests.get(f"{GEMINI_ROOT}/models",
                            params={"key": _gemini_key()}, timeout=15)
        resp.raise_for_status()
        usable = [
            m["name"].removeprefix("models/")
            for m in resp.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
        for want in ("gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"):
            if want in usable:
                return want
        if usable:
            return usable[0]
    except Exception:
        pass
    return GEMINI_MODEL


def _ask_gemini(facts: str, question: str) -> str:
    import requests
    model = _gemini_model()
    resp = requests.post(
        f"{GEMINI_ROOT}/models/{model}:generateContent",
        params={"key": _gemini_key()},
        json={
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{
                "text": f"Reconciliation facts:\n\n{facts}\n\nQuestion: {question}"
            }]}],
            # Gemini 2.5 spends part of the output budget on reasoning, so a
            # small cap truncates the answer mid-sentence rather than erroring.
            "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
        },
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"Gemini returned {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {str(body)[:200]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def _ask_anthropic(facts: str, question: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": (
            f"Reconciliation facts:\n\n{facts}\n\nQuestion: {question}"
        )}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


def ask(result: ReconResult, question: str) -> Answer:
    """Answer a question using only what this reconciliation established."""
    context, n_rows = select_context(result, question)
    which = provider()

    if which is None:
        return _deterministic_answer(result, question, context)

    facts = "\n".join(context)
    try:
        if which == "anthropic":
            text, name = _ask_anthropic(facts, question), ANTHROPIC_MODEL
        else:
            text, name = _ask_gemini(facts, question), _gemini_model()
    except Exception as exc:
        fallback = _deterministic_answer(result, question, context)
        fallback.text += f"\n\n({which} call failed: {exc})"
        return fallback

    if not text:
        return _deterministic_answer(result, question, context)
    return Answer(text=text, rows_used=n_rows, grounded=True, model=name)
