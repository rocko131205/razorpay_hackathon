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
from .models import SELF_RESOLVING, ExceptionCode, ReconResult

ANTHROPIC_MODEL = "claude-opus-5"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"
MAX_ROWS = 40
_SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

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
- Be brief: at most 120 words. Lead with the single biggest driver and its figure, then name at most three more items by id. Never enumerate every row — the exception queue already lists them. No headings."""


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

# A question naming a kind of problem is as specific as one naming an id, and it
# was previously invisible to retrieval: "how much am I overcharged in fees?"
# fell back to the largest-amounts pool, where fee rows — being small — never
# appeared. The model then correctly reported it could not answer, which is the
# grounding working and the retrieval failing.
_TOPICS: dict[str, set[ExceptionCode]] = {
    "fee": {ExceptionCode.FEE_MISMATCH},
    "overcharg": {ExceptionCode.FEE_MISMATCH},
    "commission": {ExceptionCode.FEE_MISMATCH},
    "chargeback": {ExceptionCode.CHARGEBACK},
    "disput": {ExceptionCode.CHARGEBACK},
    "refund": {ExceptionCode.PARTIAL_REFUND, ExceptionCode.FULL_REFUND},
    "duplicate": {ExceptionCode.DUPLICATE_SETTLEMENT},
    "twice": {ExceptionCode.DUPLICATE_SETTLEMENT},
    "ambigu": {ExceptionCode.AMBIGUOUS_MATCH},
    "orphan": {ExceptionCode.ORPHAN_PAYMENT},
    "receipt": {ExceptionCode.MISSING_RECEIPT},
    "split": {ExceptionCode.SPLIT_CAPTURE},
    "part captur": {ExceptionCode.SPLIT_CAPTURE},
    "phantom": {ExceptionCode.UNRESOLVED_NO_ROW},
    "unresolved": {ExceptionCode.UNRESOLVED_NO_ROW},
    "not settled": {ExceptionCode.UNRESOLVED_NO_ROW},
    "late": {ExceptionCode.LATE_SETTLEMENT},
    "bank": {ExceptionCode.SETTLEMENT_SHORTFALL},
    "shortfall": {ExceptionCode.SETTLEMENT_SHORTFALL},
    "weak": {ExceptionCode.MISSING_RECEIPT},
}


def _mentioned_topics(question: str) -> set[str]:
    q = question.lower()
    hits: set[str] = set()
    for word, codes in _TOPICS.items():
        if word in q:
            hits.update(c.value for c in codes)
    return hits


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

    # Totals per type, computed here so that a "how much" question can always be
    # answered from the context. Previously only counts were supplied, so the
    # model had the number of fee mismatches but not their value — and correctly
    # refused to answer rather than add them up itself.
    per_code: dict[str, list] = {}
    for e in result.exceptions:
        row = per_code.setdefault(e.code.value, [0, 0.0])
        row[0] += 1
        row[1] += e.amount_at_risk
    lines.append("EXCEPTION TOTALS BY TYPE: " + "; ".join(
        f"{code} = {n} exceptions, ₹{amt:,.2f} at risk in total"
        for code, (n, amt) in sorted(per_code.items())))

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

    # Narrow by whatever the question actually named — an id, a kind of problem,
    # or both. With neither, the largest amounts are the right default, since a
    # shortfall question is nearly always about them.
    topics = _mentioned_topics(question)
    pool = [e for e in result.exceptions
            if (not wanted or e.order_id in wanted or e.payment_id in wanted)
            and (not topics or e.code.value in topics)]
    if not pool:
        # The narrowing matched nothing. Sending an empty context would produce
        # "the facts do not say", which is true but useless; the whole queue is
        # a better answer to a question about something that is not in it.
        pool = list(result.exceptions)

    # Ordered exactly as the exception queue orders it — severity first, then
    # money. Sorting by amount alone led the model to call the largest total the
    # top priority, which contradicted the queue the controller is looking at.
    pool.sort(key=lambda e: (_SEV_RANK[e.severity], -e.amount_at_risk))
    lines.append(
        "PRIORITY RULE: severity outranks amount (CRITICAL, then HIGH, MEDIUM, "
        "LOW). Rows below are already in priority order."
    )

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


def _ask_gemini(facts: str, question: str, system: str = SYSTEM,
                thinking_budget: int | None = None) -> str:
    import requests
    model = _gemini_model()
    resp = requests.post(
        f"{GEMINI_ROOT}/models/{model}:generateContent",
        params={"key": _gemini_key()},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{
                "text": f"Reconciliation facts:\n\n{facts}\n\nQuestion: {question}"
            }]}],
            # Gemini 2.5 spends part of the output budget on reasoning, so a
            # small cap truncates the answer mid-sentence rather than erroring.
            # Triage is ranking already-computed totals, not reasoning, so the
            # brief turns that budget off — it cut a 21s call to about 5s, which
            # is the difference between a usable button and a stalled demo.
            "generationConfig": (
                {"temperature": 0, "maxOutputTokens": 8192}
                if thinking_budget is None else
                {"temperature": 0, "maxOutputTokens": 8192,
                 "thinkingConfig": {"thinkingBudget": thinking_budget}}
            ),
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


def _ask_anthropic(facts: str, question: str, system: str = SYSTEM) -> str:
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=system,
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
            # Same reasoning here as for the brief: the retrieval layer has
            # already decided what is relevant, so the model is summarising
            # rather than working anything out. Leaving the budget on cost 28s
            # and produced a 272-word list of every exception type.
            text, name = (_ask_gemini(facts, question, thinking_budget=0),
                          _gemini_model())
    except Exception as exc:
        fallback = _deterministic_answer(result, question, context)
        fallback.text += f"\n\n({which} call failed: {exc})"
        return fallback

    if not text:
        return _deterministic_answer(result, question, context)
    return Answer(text=text, rows_used=n_rows, grounded=True, model=name)


# ---------------------------------------------------------------------------
# Morning brief — triage, not explanation
# ---------------------------------------------------------------------------
#
# The Q&A layer above answers a question the controller already thought to ask.
# This answers the one they open the queue with: "what do I actually do first?"
#
# It is the same division of labour. Python computes every total and does every
# ranking; the model decides what to say about them and what can wait. Grouping
# work that is really one task — twelve fee mismatches are one conversation with
# Razorpay, not twelve jobs — is judgement, and it is the only thing being asked
# for here.

BRIEF_SYSTEM = """You triage a settlement reconciliation exception queue for a \
merchant's finance controller who has limited time this morning.

You are given aggregates and individual rows that a deterministic engine already \
computed. Say what to do first, what can be batched, and what can wait.

Rules you must follow:
- Never calculate. Every figure you cite must appear verbatim in the facts.
- Group work that is really one task, and say so.
- Name what can be ignored today, and why.
- An exception listing possible causes has not been diagnosed. Never assert a cause.
- No preamble, no headings, no bullet points. Three or four sentences, at most 110 \
words, written the way a colleague would say it out loud."""

BRIEF_ITEMS = 12


def _brief_facts(result: ReconResult) -> list[str]:
    """Aggregate the queue for triage.

    Every total here is computed in Python. The model is handed finished
    arithmetic so that, as in `select_context`, it has nothing to add up.
    """
    lines = [
        f"RUN: {len(result.matches)} of {result.orders_seen} orders matched "
        f"({result.match_rate:.1f}%). {len(result.exceptions)} exceptions, "
        f"₹{result.amount_at_risk:,.2f} at risk."
    ]

    groups: dict[str, list] = {}
    for e in result.exceptions:
        groups.setdefault(e.code.value, []).append(e)

    ranked = []
    for code, items in groups.items():
        total = round(sum(i.amount_at_risk for i in items), 2)
        severity = items[0].severity
        tail = (" — this type usually resolves itself once the next cycle's rows arrive"
                if items[0].code in SELF_RESOLVING else "")
        ranked.append((
            _SEV_RANK[severity], -total,
            f"GROUP {code} [{severity}] {len(items)} items, "
            f"₹{total:,.2f} at risk in total{tail}"
        ))
    ranked.sort()
    lines.extend(row[2] for row in ranked)

    top = sorted(result.exceptions,
                 key=lambda e: (_SEV_RANK[e.severity], -e.amount_at_risk))[:BRIEF_ITEMS]
    for e in top:
        ref = e.order_id or e.payment_id or "—"
        lines.append(f"ITEM {e.code.value} [{e.severity}] {ref} "
                     f"₹{e.amount_at_risk:,.2f} — {e.detail}")
    return lines


def _deterministic_brief(result: ReconResult, context: list[str]) -> Answer:
    """The same triage without a model. Ranked, grouped, and honest about it."""
    if not result.exceptions:
        return Answer(text="Nothing was raised this cycle.", rows_used=len(context),
                      grounded=False)

    groups: dict[str, list] = {}
    for e in result.exceptions:
        groups.setdefault(e.code.value, []).append(e)
    ranked = sorted(
        groups.items(),
        key=lambda kv: (_SEV_RANK[kv[1][0].severity],
                        -sum(i.amount_at_risk for i in kv[1])),
    )

    head_code, head_items = ranked[0]
    head_total = round(sum(i.amount_at_risk for i in head_items), 2)
    parts = [
        f"{len(result.exceptions)} exceptions this cycle, ₹{result.amount_at_risk:,.2f} "
        f"at risk in total.",
        f"Start with {head_code} — {len(head_items)} items, ₹{head_total:,.2f}, "
        f"{head_items[0].severity} severity.",
    ]
    if len(ranked) > 1:
        nxt = ", ".join(
            f"{code} ({len(items)})" for code, items in ranked[1:4]
        )
        parts.append(f"Then: {nxt}.")
    self_res = [e for e in result.exceptions if e.code in SELF_RESOLVING]
    if self_res:
        parts.append(f"{len(self_res)} are expected to close on the next cycle and "
                     f"need nothing today.")

    return Answer(
        text=" ".join(parts) + "\n\n(Triaged from the ledger directly — no model "
                               "key configured.)",
        rows_used=len(context),
        grounded=False,
    )


def brief(result: ReconResult) -> Answer:
    """Triage the exception queue into a few sentences a controller can act on."""
    context = _brief_facts(result)
    which = provider()

    if which is None:
        return _deterministic_brief(result, context)

    facts = "\n".join(context)
    ask_for = "Write the morning brief for this queue."
    try:
        if which == "anthropic":
            text = _ask_anthropic(facts, ask_for, BRIEF_SYSTEM)
            name = ANTHROPIC_MODEL
        else:
            text = _ask_gemini(facts, ask_for, BRIEF_SYSTEM, thinking_budget=0)
            name = _gemini_model()
    except Exception as exc:
        fallback = _deterministic_brief(result, context)
        fallback.text += f"\n\n({which} call failed: {exc})"
        return fallback

    if not text:
        return _deterministic_brief(result, context)
    return Answer(text=text, rows_used=len(context), grounded=True, model=name)
