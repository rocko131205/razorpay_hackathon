"""Render helpers for the reconciliation dashboard.

Every function here takes engine output and returns markup. None of them
compute anything — the numbers arrive already decided by `recon/`, which keeps
the display layer incapable of disagreeing with the audit trail.
"""
from __future__ import annotations

import html
from pathlib import Path

import streamlit as st

from recon.models import Exception_, Match, ReconResult

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

_TIER_CLASS = {
    "T1_RECEIPT": "t1",
    "T2_PAYMENT_ID": "t1",
    "T3_AMOUNT_PHONE": "t3",
    "T4_AMOUNT_ONLY": "t4",
}
_TIER_LABEL = {
    "T1_RECEIPT": "T1 · RECEIPT",
    "T2_PAYMENT_ID": "T2 · PAYMENT ID",
    "T3_AMOUNT_PHONE": "T3 · AMOUNT+PHONE",
    "T4_AMOUNT_ONLY": "T4 · AMOUNT ONLY",
}


def load_css() -> None:
    css = Path(__file__).parent / "styles.css"
    if css.exists():
        st.markdown(f"<style>{css.read_text(encoding='utf-8')}</style>",
                    unsafe_allow_html=True)


def top_bar(settlement: str, utr: str) -> None:
    st.markdown(
        f'<div class="rc-topbar">'
        f'  <div class="rc-brand">'
        f'    <div class="rc-mark">RC</div>'
        f'    <div><div class="rc-title">SETTLEMENT RECON</div>'
        f'    <div class="rc-sub">Reconciliation Controller</div></div>'
        f'  </div>'
        f'  <div class="rc-topright">'
        f'    <span>CYCLE <b>{html.escape(settlement)}</b></span>'
        f'    <span>UTR <b>{html.escape(utr)}</b></span>'
        f'  </div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def section(title: str, subtitle: str = "") -> None:
    sub = f'<div class="rc-section-sub">{html.escape(subtitle)}</div>' if subtitle else ""
    st.markdown(
        f'<div class="rc-section"><div class="rc-section-title">{html.escape(title)}</div>'
        f'{sub}</div>',
        unsafe_allow_html=True,
    )


def hr() -> None:
    st.markdown('<hr class="rc-hr">', unsafe_allow_html=True)


def hero(result: ReconResult) -> None:
    """The one number a controller opens this page to see."""
    st.markdown(
        f'<div class="rc-hero">'
        f'  <div><div class="rc-hero-num">{result.match_rate:.1f}%</div></div>'
        f'  <div>'
        f'    <div class="rc-hero-lbl">Match rate</div>'
        f'    <div class="rc-hero-det">'
        f'      {len(result.matches):,} of {result.orders_seen:,} orders paired to a settled row'
        f'      &nbsp;·&nbsp; {result.orders_seen - len(result.matches)} escalated'
        f'    </div>'
        f'    <div class="rc-hero-det" style="margin-top:4px;color:var(--c-text3);">'
        f'      {result.payments_seen:,} settled rows read in '
        f'{result.elapsed_seconds * 1000:.0f} ms '
        f'({result.throughput:,.0f} orders/sec)'
        f'    </div>'
        f'  </div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def pending_hero(result: ReconResult, n_orders: int) -> None:
    """Live account with no payout history yet.

    Every order is awaiting settlement, which is a legitimate finance-ops state
    and not a reconciliation failure — so it is reported as a count rather than
    as a match rate of zero.
    """
    st.markdown(
        f'<div class="rc-hero">'
        f'  <div><div class="rc-hero-num">{n_orders}</div></div>'
        f'  <div>'
        f'    <div class="rc-hero-lbl">Orders awaiting settlement</div>'
        f'    <div class="rc-hero-det">'
        f'      Read live from Razorpay. Nothing has been paid out against them yet, '
        f'      so there is no settled row to match and no match rate to report.'
        f'    </div>'
        f'    <div class="rc-hero-det" style="margin-top:4px;color:var(--c-text3);">'
        f'      ₹{result.amount_at_risk:,.2f} outstanding &nbsp;·&nbsp; every order '
        f'carried forward to the next cycle'
        f'    </div>'
        f'  </div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def stat_tiles(result: ReconResult) -> None:
    crit = sum(1 for e in result.exceptions if e.severity == "CRITICAL")
    # LOW severity means "not urgent", which is not the same as "will resolve
    # itself" — a full refund has already happened and resolves nothing. The
    # findings that genuinely close on their own are the carried-forward ones,
    # and those are HIGH. Labelling this tile as self-resolving conflated the two.
    low = sum(1 for e in result.exceptions if e.severity == "LOW")
    tiles = [
        ("EXCEPTIONS", f"{len(result.exceptions)}", "raised this cycle", "warn"),
        ("MONEY AT RISK", f"₹{result.amount_at_risk:,.0f}", "not yet proven safe", "bad"),
        ("NEEDS A HUMAN", f"{crit}", "critical severity", "bad" if crit else "good"),
        ("LOW PRIORITY", f"{low}", "low urgency · nothing due today", "good"),
    ]
    for col, (lbl, val, note, cls) in zip(st.columns(4), tiles):
        col.markdown(
            f'<div class="rc-stat {cls}">'
            f'<div class="rc-stat-lbl">{lbl}</div>'
            f'<div class="rc-stat-val">{html.escape(val)}</div>'
            f'<div class="rc-stat-note">{html.escape(note)}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )


def tier_breakdown(result: ReconResult) -> None:
    """How much of the match rate rests on strong versus weak evidence."""
    counts = result.tier_counts
    total = max(sum(counts.values()), 1)
    for tier in ("T1_RECEIPT", "T2_PAYMENT_ID", "T3_AMOUNT_PHONE", "T4_AMOUNT_ONLY"):
        n = counts.get(tier, 0)
        if not n:
            continue
        pct = n / total * 100
        st.markdown(
            f'<div class="rc-tier">'
            f'  <div class="rc-tier-head">'
            f'    <span>{_TIER_LABEL[tier]}</span><span>{n} · {pct:.1f}%</span>'
            f'  </div>'
            f'  <div class="rc-tier-bar {_TIER_CLASS[tier]}"><i style="width:{pct:.1f}%"></i></div>'
            f'</div>',
            unsafe_allow_html=True,
        )


def exception_row(e: Exception_) -> None:
    ref = e.order_id or e.payment_id or "—"
    sev = e.severity.lower()
    causes = "".join(f"<li>{html.escape(c)}</li>" for c in e.possible_causes)
    causes_html = f'<ul class="rc-exc-causes">{causes}</ul>' if causes else ""
    amount = (f'<span class="rc-exc-amt">₹{e.amount_at_risk:,.2f}</span>'
              if e.amount_at_risk else "")
    st.markdown(
        f'<div class="rc-exc {sev}">'
        f'  <div class="rc-exc-head">'
        f'    <span class="rc-chip {sev}">{html.escape(e.severity)}</span>'
        f'    <span class="rc-exc-code">{html.escape(e.code.value)}</span>'
        f'    <span class="rc-exc-ref">{html.escape(ref)}</span>'
        f'    {amount}'
        f'  </div>'
        f'  <div class="rc-exc-detail">{html.escape(e.detail)}</div>'
        f'  {causes_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


def audit_row(m: Match) -> None:
    cls = _TIER_CLASS.get(m.tier.value, "")
    st.markdown(
        f'<div class="rc-audit">'
        f'  <span class="oid">{html.escape(m.order_id)}</span>'
        f'  <span class="tier {cls}">{_TIER_LABEL.get(m.tier.value, m.tier.value)}</span>'
        f'  <span class="rule">{html.escape(m.rule)}</span>'
        f'  <span class="amt">₹{m.net_to_bank:,.2f}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )


def inferred_table(per_type: dict) -> None:
    """Checks that reach a conclusion the data does not state outright."""
    rows = []
    for code, m in sorted(per_type.items()):
        rows.append(
            f'<tr><td>{html.escape(code)}</td>'
            f'<td>{m["planted"]}</td><td>{m["raised"]}</td>'
            f'<td>{m["precision"]:.2f}</td><td>{m["recall"]:.2f}</td>'
            f'<td class="{"miss" if m["fp"] else "ok"}">{m["fp"]}</td>'
            f'<td class="{"miss" if m["fn"] else "ok"}">{m["fn"]}</td></tr>'
        )
    st.markdown(
        '<table class="rc-mtable"><thead><tr>'
        '<th>Check</th><th>Planted</th><th>Found</th>'
        '<th>Precision</th><th>Recall</th><th>False alarms</th><th>Missed</th>'
        '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>',
        unsafe_allow_html=True,
    )


def labelled_table(per_type: dict) -> None:
    """Checks that report a field Razorpay already populated.

    Deliberately shown without precision or recall. Scoring a check that
    copies `type` would claim credit for reading a column.
    """
    rows = "".join(
        f'<tr><td>{html.escape(code)}</td><td>{m["raised"]} of {m["planted"]}</td>'
        f'<td style="color:var(--c-text3);">read from <code>type</code></td></tr>'
        for code, m in sorted(per_type.items())
    )
    st.markdown(
        '<table class="rc-mtable"><thead><tr>'
        '<th>Check</th><th>Reported</th><th>Source</th>'
        '</tr></thead><tbody>' + rows + '</tbody></table>',
        unsafe_allow_html=True,
    )


def loop_table(res: dict) -> None:
    """Findings that could not be decided today, and what became of them."""
    rows = [
        ("Carried forward from cycle 1", res["carried_forward"], ""),
        ("Closed once the next cycle's rows arrived", res["closed_next_cycle"],
         f'{res["closed_correctly"]} correctly · {res["closed_wrongly"]} wrongly'),
        ("Still open after cycle 2", res["still_open"],
         f'{res["remaining_are_real_phantoms"]} are genuine phantoms'),
    ]
    body = "".join(
        f'<tr><td>{html.escape(label)}</td><td>{n}</td>'
        f'<td style="color:var(--c-text3);">{html.escape(note)}</td></tr>'
        for label, n, note in rows
    )
    st.markdown(
        '<table class="rc-mtable"><thead><tr><th>Stage</th><th>Orders</th>'
        '<th></th></tr></thead><tbody>' + body + '</tbody></table>',
        unsafe_allow_html=True,
    )


def rank_exceptions(exceptions: list[Exception_]) -> list[Exception_]:
    """Money at risk first, within severity — the order a controller works in."""
    return sorted(exceptions,
                  key=lambda e: (SEVERITY_ORDER[e.severity], -e.amount_at_risk))


def brief_card(answer) -> None:
    """The triage paragraph, labelled with what produced it.

    The row count and model name are shown for the same reason the Ask page
    shows its context: an answer a controller cannot trace is an answer they
    cannot act on.
    """
    src = answer.model if answer.model else "ledger only — no model key"
    st.markdown(
        f'<div style="font-size:10px;color:var(--c-text3);letter-spacing:0.1em;'
        f'margin:14px 0 6px;">MORNING BRIEF &nbsp;·&nbsp; '
        f'{answer.rows_used} LEDGER ROWS CONSULTED &nbsp;·&nbsp; '
        f'{html.escape(str(src)).upper()}</div>'
        f'<div class="rc-exc" style="border-left-color:var(--c-accent);">'
        f'<div class="rc-exc-detail" style="white-space:pre-wrap;">'
        f'{html.escape(answer.text)}</div></div>',
        unsafe_allow_html=True,
    )
