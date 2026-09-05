"""Settlement Recon — reconciliation controller dashboard.

Four screens, in the order a finance controller uses them:

  Run          trigger a cycle, see the match rate resolve
  Exceptions   the work queue, money at risk first
  Audit Trail  every match and the rule that produced it
  Accuracy     how the engine scores against known ground truth

The dashboard never computes. It renders what `recon/` decided, so the screen
and the audit trail cannot disagree.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import streamlit as st

from recon.matcher import load_bank, load_orders, load_payments, reconcile
from recon.metrics import resolution, score
from recon.models import SEVERITY, ExceptionCode
from recon import qa_agent, razorpay_client
from ui import components as ui

DATA = Path("data")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def ensure_data(n_orders: int, seed: int) -> dict:
    if not (DATA / "ground_truth.json").exists():
        from recon.generator import generate
        return generate(n_orders=n_orders, seed=seed, out_dir=DATA)
    return json.loads((DATA / "ground_truth.json").read_text(encoding="utf-8"))


def run_live(truth: dict):
    """Reconcile against whatever this Razorpay account actually holds.

    The engine is unchanged — live rows arrive in the same shapes the generator
    produces, so nothing downstream knows or cares where they came from.
    """
    from datetime import date
    today = date.today()
    live = razorpay_client.fetch_live(today.year, today.month)
    result = reconcile(
        live.orders, live.payments, [],          # no bank file for live data
        settlement_utr=(live.payments[0].settlement_utr if live.payments else ""),
        fee_rate=truth["fee_rate"], gst_on_fee=truth["gst_on_fee"],
    )
    return result, live


def run_cycle(truth: dict, cycle: int = 1):
    """Reconcile one cycle.

    Cycle two sees everything cycle one saw plus the rows that had not been
    reported yet, which is what lets carried-forward findings close.
    """
    payments = load_payments(DATA / "razorpay_recon.csv")
    bank_path = DATA / "bank_statement.csv"
    utr = truth["settlement_utr"]
    prior: tuple[str, ...] = ()

    if cycle == 2:
        nxt = DATA / "razorpay_recon_cycle2.csv"
        if nxt.exists():
            payments = payments + load_payments(nxt)
        b2 = DATA / "bank_statement_cycle2.csv"
        if b2.exists():
            bank_path = b2
        utr = truth["next_cycle_utr"]
        # Cycle one's payout has already landed, so its rows are settled rather
        # than late. Without this every row from the first report is re-flagged.
        prior = (truth["settlement_utr"],)

    return reconcile(
        load_orders(DATA / "orders.csv"), payments, load_bank(bank_path),
        settlement_utr=utr,
        prior_utrs=prior,
        fee_rate=truth["fee_rate"],
        gst_on_fee=truth["gst_on_fee"],
    )


def _clear_derived() -> None:
    """Drop anything computed from a previous run.

    Briefs and answers are cached per cycle so switching views does not re-call
    the model. A new reconciliation makes all of them stale at once.
    """
    for key in ("brief_1", "brief_2", "answer_1", "answer_2",
                "asked_1", "asked_2"):
        st.session_state.pop(key, None)
    st.session_state["view_cycle"] = 1


def current_result():
    """The cycle the user has chosen to look at, and its number.

    Cycle two is stored alongside cycle one rather than replacing it, because
    the resolution table needs both side by side. The side effect was that every
    other screen stayed permanently on cycle one — the queue, the audit trail
    and the Q&A could not see the loop close. This is the switch.
    """
    if st.session_state.get("view_cycle") == 2:
        r2 = st.session_state.get("result2")
        if r2 is not None:
            return r2, 2
    return st.session_state.get("result"), 1


def cycle_badge(cycle: int) -> None:
    """Say which cycle is on screen, but only when it is not the obvious one."""
    if cycle == 1:
        return
    st.markdown(
        '<div style="font-size:10px;color:var(--c-green);letter-spacing:0.1em;'
        'margin:-4px 0 10px;">▸ VIEWING CYCLE 2 — the next payout\'s rows have '
        'arrived</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def page_run(truth: dict) -> None:
    ui.section("Reconciliation Run",
               f"Merchant orders against Razorpay settled rows and the bank credit "
               f"for {truth['settlement_date']}")

    st.markdown(
        '<div style="font-size:11px;color:var(--c-text3);line-height:1.8;">'
        'Three sources, one cycle. Orders come from the merchant\'s own system, '
        'settled rows from Razorpay\'s recon report, and a single lump credit '
        'from the bank. Matching is deterministic — no model is consulted, '
        'because a match rate is only meaningful if it is reproducible.'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.session_state.get("source") == "live":
        if st.button("▶  RECONCILE LIVE RAZORPAY DATA", key="run_live_btn"):
            try:
                with st.spinner("Reading from Razorpay test mode…"):
                    result, live = run_live(truth)
                st.session_state["result"] = result
                st.session_state["live"] = live
                st.session_state.pop("result2", None)
                _clear_derived()
            except razorpay_client.RazorpayError as exc:
                st.error(str(exc))

        live = st.session_state.get("live")
        if live is not None:
            st.markdown(
                f'<div style="margin:12px 0;font-size:11px;color:var(--c-text2);'
                f'border-left:2px solid var(--c-green);padding-left:12px;line-height:1.7;">'
                f'<b style="color:var(--c-green);">Live · {html.escape(live.source)}</b><br>'
                f'{len(live.orders)} orders and {len(live.payments)} payment rows read '
                f'from Razorpay test mode.<br>'
                f'<span style="color:var(--c-text3);">{html.escape(live.note)}</span></div>',
                unsafe_allow_html=True,
            )
    else:
        _run_buttons(truth)

    result = st.session_state.get("result")
    if result is not None and st.session_state.get("source") == "live":
        live = st.session_state.get("live")
        ui.hr()
        # A match rate is only meaningful once something has settled. Before
        # that, reporting 0% would read as a failure when the true state is
        # simply that no payout has run yet.
        if live is not None and not live.settled and not live.payments:
            ui.pending_hero(result, len(live.orders))
        else:
            ui.hero(result)
            st.write("")
            ui.stat_tiles(result)
            ui.hr()
            ui.section("Evidence Behind the Match Rate",
                       "Which rule paired each order — strong keys first, weak ones flagged")
            ui.tier_breakdown(result)
        return
    if result is None:
        st.markdown(
            '<div style="color:var(--c-text3);font-size:11px;margin-top:14px;">'
            '▸ Awaiting run…</div>', unsafe_allow_html=True)
        return
    _render_synthetic_run(truth, result)


def _run_buttons(truth: dict) -> None:
    c1, c2, _ = st.columns([1, 1, 2])
    if c1.button("▶  RUN CYCLE 1", key="run_btn", use_container_width=True):
        st.session_state["result"] = run_cycle(truth, cycle=1)
        st.session_state["cycle"] = 1
        st.session_state.pop("result2", None)
        _clear_derived()
    if c2.button("▶▶  RUN NEXT CYCLE", key="run_btn2", use_container_width=True,
                 disabled="result" not in st.session_state,
                 help="Tomorrow's report arrives. Carried-forward findings resolve or harden."):
        st.session_state["result2"] = run_cycle(truth, cycle=2)
        st.session_state["cycle"] = 2


def _render_synthetic_run(truth: dict, result) -> None:
    ui.hr()
    ui.hero(result)
    st.write("")
    ui.stat_tiles(result)

    ui.hr()
    ui.section("Evidence Behind the Match Rate",
               "Which rule paired each order — strong keys first, weak ones flagged")
    ui.tier_breakdown(result)

    unmatched = result.orders_seen - len(result.matches)
    if unmatched:
        ambiguous = sum(1 for e in result.exceptions
                        if e.code is ExceptionCode.AMBIGUOUS_MATCH)
        pending = sum(1 for e in result.exceptions
                      if e.code is ExceptionCode.UNRESOLVED_NO_ROW)
        st.markdown(
            f'<div style="margin-top:14px;font-size:11px;color:var(--c-text2);'
            f'border-left:2px solid var(--c-red);padding-left:12px;line-height:1.7;">'
            f'<b style="color:var(--c-red);">{unmatched} orders were not matched.</b><br>'
            f'{ambiguous} are ambiguous — several settled rows are equally consistent '
            f'with them, and nothing separates the candidates. '
            f'{pending} have no settled row in this cycle at all, which today is '
            f'equally consistent with a late settlement and with a payment that '
            f'never happened.<br>'
            f'<span style="color:var(--c-text3);">Both are escalated rather than '
            f'guessed. Run the next cycle to see which resolve.</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    r2 = st.session_state.get("result2")
    if r2 is not None:
        ui.hr()
        ui.section("Closing the Loop",
                   "The next cycle's rows arrive — findings resolve or harden")
        res = resolution(result, r2, truth)
        ui.loop_table(res)
        st.markdown(
            f'<div style="margin-top:12px;font-size:11px;color:var(--c-text2);'
            f'border-left:2px solid var(--c-green);padding-left:12px;line-height:1.7;">'
            f'Match rate moved from <b>{result.match_rate:.1f}%</b> to '
            f'<b style="color:var(--c-green);">{r2.match_rate:.1f}%</b> without a single '
            f'rule changing. The engine did not become smarter — it stopped being asked '
            f'to answer a question the data could not yet support.'
            f'</div>',
            unsafe_allow_html=True,
        )


def page_exceptions() -> None:
    result, cycle = current_result()
    ui.section("Exception Queue", "Highest severity first, then largest amount at risk")
    cycle_badge(cycle)
    if result is None:
        st.markdown('<div style="color:var(--c-text3);font-size:11px;">'
                    '▸ Run a reconciliation first.</div>', unsafe_allow_html=True)
        return

    _morning_brief(result, cycle)

    ranked = ui.rank_exceptions(result.exceptions)
    all_codes = sorted({e.code.value for e in ranked})

    c1, c2 = st.columns([2, 1])
    chosen = c1.multiselect("Filter by type", all_codes, default=[],
                            label_visibility="collapsed",
                            placeholder="All exception types")
    severities = c2.multiselect("Severity", ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                                default=[], label_visibility="collapsed",
                                placeholder="All severities")

    shown = [e for e in ranked
             if (not chosen or e.code.value in chosen)
             and (not severities or e.severity in severities)]

    at_risk = sum(e.amount_at_risk for e in shown)
    st.markdown(
        f'<div style="font-size:10px;color:var(--c-text3);letter-spacing:0.08em;'
        f'margin:10px 0 12px;">SHOWING {len(shown)} OF {len(ranked)} '
        f'· ₹{at_risk:,.2f} AT RISK</div>',
        unsafe_allow_html=True,
    )
    for e in shown:
        ui.exception_row(e)


def _morning_brief(result, cycle: int = 1) -> None:
    """Triage the queue before the controller starts working it.

    The engine already ranked and totalled everything; what the model adds is
    judgement about what can be batched and what can wait. It is handed finished
    arithmetic and forbidden from calculating, exactly as the Q&A layer is — so
    the brief can be checked line by line against the queue below it.
    """
    which = qa_agent.provider()
    c1, c2 = st.columns([1, 3])
    if c1.button("◆  MORNING BRIEF", key="brief_btn", use_container_width=True,
                 help="What to do first, what can be batched, what can wait"):
        with st.spinner("Triaging the queue…"):
            st.session_state[f"brief_{cycle}"] = qa_agent.brief(result)
    c2.markdown(
        f'<div style="font-size:10px;color:var(--c-text3);line-height:2.6;'
        f'letter-spacing:0.04em;">Ranking and totals are computed here; the model '
        f'only decides what to say about them &nbsp;·&nbsp; '
        f'{html.escape(which) if which else "no model key — triaged from the ledger"}'
        f'</div>',
        unsafe_allow_html=True,
    )

    answer = st.session_state.get(f"brief_{cycle}")
    if answer is None:
        return
    ui.brief_card(answer)
    with st.expander("What the model was allowed to see"):
        st.code("\n".join(qa_agent._brief_facts(result)), language="text")


def page_audit() -> None:
    result, cycle = current_result()
    ui.section("Audit Trail", "Every pairing the engine made, and the rule that made it")
    cycle_badge(cycle)
    if result is None:
        st.markdown('<div style="color:var(--c-text3);font-size:11px;">'
                    '▸ Run a reconciliation first.</div>', unsafe_allow_html=True)
        return

    q = st.text_input("Search", placeholder="Order id, payment id or rule…",
                      label_visibility="collapsed")
    rows = result.matches
    if q:
        needle = q.strip().lower()
        rows = [m for m in rows
                if needle in m.order_id.lower()
                or needle in m.payment_id.lower()
                or needle in m.rule.lower()]

    st.markdown(
        f'<div style="font-size:10px;color:var(--c-text3);letter-spacing:0.08em;'
        f'margin:8px 0 6px;">{len(rows)} MATCHES</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="rc-audit" style="border-bottom:1px solid var(--c-border2);'
        'color:var(--c-text3);font-size:8.5px;letter-spacing:0.14em;">'
        '<span>ORDER</span><span>RULE TIER</span><span>EVIDENCE</span>'
        '<span>NET TO BANK</span></div>',
        unsafe_allow_html=True,
    )
    for m in rows[:400]:
        ui.audit_row(m)
    if len(rows) > 400:
        st.markdown(f'<div style="font-size:10px;color:var(--c-text3);padding:8px 12px;">'
                    f'…{len(rows) - 400:,} more. Narrow the search to see them.</div>',
                    unsafe_allow_html=True)


def page_accuracy(truth: dict) -> None:
    result, cycle = current_result()
    ui.section("Measured Accuracy",
               "Scored against the defects the generator planted — including the misses")
    if result is None:
        st.markdown('<div style="color:var(--c-text3);font-size:11px;">'
                    '▸ Run a reconciliation first.</div>', unsafe_allow_html=True)
        return

    # Scoring needs an answer key, and only the generator produces one. Grading a
    # live run against the synthetic key would compare unrelated records and print
    # numbers that mean nothing, so the page declines rather than inventing them.
    if st.session_state.get("source") == "live":
        st.markdown(
            '<div style="font-size:11px;color:var(--c-text2);line-height:1.8;'
            'border-left:2px solid var(--c-accent);padding-left:12px;">'
            '<b style="color:var(--c-accent);">Accuracy is not defined for live data.</b><br>'
            'Measuring accuracy requires an answer key — a record of which defects are '
            'genuinely present. The generator can write one because it planted them. '
            'Nobody has one for a real merchant\'s settlements, which is precisely why '
            'this tool exists.<br>'
            '<span style="color:var(--c-text3);">Switch the data source to Synthetic '
            'to see measured accuracy.</span></div>',
            unsafe_allow_html=True)
        return

    cycle_badge(cycle)
    s = score(result, truth, cycle=cycle)
    inf = s["inferred"]["overall"]
    m = s["matching"]

    tiles = [
        ("MATCH RATE", f"{m['match_rate']:.1f}%", f"{m['unmatched']} escalated", "warn"),
        ("INFERRED PRECISION", f"{inf['precision']:.3f}", f"{inf['fp']} false alarms",
         "bad" if inf["fp"] else "good"),
        ("INFERRED RECALL", f"{inf['recall']:.3f}", f"{inf['fn']} missed",
         "bad" if inf["fn"] else "good"),
        ("THROUGHPUT", f"{result.throughput:,.0f}/s", "orders per second", "good"),
    ]
    for col, (lbl, val, note, cls) in zip(st.columns(4), tiles):
        col.markdown(
            f'<div class="rc-stat {cls}"><div class="rc-stat-lbl">{lbl}</div>'
            f'<div class="rc-stat-val">{val}</div>'
            f'<div class="rc-stat-note">{note}</div></div>',
            unsafe_allow_html=True,
        )

    ui.hr()
    ui.section("Inferred Checks",
               "Conclusions the engine worked out — these are scored")
    ui.inferred_table(s["inferred"]["per_type"])

    ui.hr()
    ui.section("Reported, Not Inferred",
               "Checks that repeat a field Razorpay already populated")
    ui.labelled_table(s["labelled"]["per_type"])
    st.markdown(
        '<div style="margin-top:10px;font-size:11px;color:var(--c-text2);'
        'border-left:2px solid var(--c-accent);padding-left:12px;line-height:1.7;">'
        'These are deliberately shown without precision or recall. Razorpay\'s recon '
        'report carries a <code>type</code> column, so detecting a chargeback means '
        'reading it. Such a check cannot be wrong, and scoring it would inflate the '
        'headline number with something that was never in doubt.'
        '</div>',
        unsafe_allow_html=True,
    )

    r2 = st.session_state.get("result2")
    if r2 is not None:
        ui.hr()
        ui.section("Resolution Across Cycles",
                   "What became of the findings that could not be decided on day one")
        ui.loop_table(resolution(result, r2, truth))


def page_ask() -> None:
    result, cycle = current_result()
    ui.section("Ask the Ledger",
               "Questions answered only from what this reconciliation established")
    cycle_badge(cycle)
    if result is None:
        st.markdown('<div style="color:var(--c-text3);font-size:11px;">'
                    '▸ Run a reconciliation first.</div>', unsafe_allow_html=True)
        return

    which = qa_agent.provider()
    model_state = (f"<b style='color:var(--c-green);'>{which}</b>" if which
                   else "not configured — answers come straight from the ledger")
    st.markdown(
        f'<div style="font-size:11px;color:var(--c-text2);line-height:1.8;'
        f'border-left:2px solid var(--c-accent);padding-left:12px;">'
        f'Python selects the relevant ledger rows before the model sees anything, '
        f'and the model is forbidden from calculating — every figure it can quote '
        f'is already computed. That is what keeps an answer checkable against the '
        f'audit trail.<br>'
        f'<span style="color:var(--c-text3);">Model: {model_state}</span></div>',
        unsafe_allow_html=True,
    )

    st.write("")
    examples = [
        "Why is the payout short this cycle?",
        "What happened to ORD-0009?",
        "Which exceptions need a human first?",
        "How much am I being overcharged in fees?",
    ]
    for col, ex in zip(st.columns(len(examples)), examples):
        if col.button(ex, use_container_width=True, key=f"ex_{abs(hash(ex))}"):
            # A keyed widget ignores a new `value` once it exists, so the
            # example has to be written into its state and the script rerun.
            st.session_state["q_input"] = ex
            st.rerun()

    question = st.text_input(
        "Question",
        placeholder="e.g. why was Tuesday's payout short?",
        label_visibility="collapsed", key="q_input",
    )
    if st.button("ASK", key="ask_btn") and question.strip():
        with st.spinner("Reading the match ledger…"):
            st.session_state[f"answer_{cycle}"] = qa_agent.ask(result, question.strip())
        st.session_state[f"asked_{cycle}"] = question.strip()

    answer = st.session_state.get(f"answer_{cycle}")
    if answer is None:
        return

    ui.hr()
    st.markdown(
        f'<div style="font-size:10px;color:var(--c-text3);letter-spacing:0.1em;'
        f'margin-bottom:8px;">ANSWER · {answer.rows_used} LEDGER ROWS CONSULTED'
        f'{" · " + answer.model if answer.model else ""}</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="rc-exc" style="border-left-color:var(--c-accent);">'
        f'<div class="rc-exc-detail" style="white-space:pre-wrap;">'
        f'{html.escape(answer.text)}</div></div>',
        unsafe_allow_html=True,
    )

    with st.expander("What the model was allowed to see"):
        context, _ = qa_agent.select_context(
            result, st.session_state.get(f"asked_{cycle}", ""))
        st.code("\n".join(context), language="text")


def main() -> None:
    st.set_page_config(page_title="Settlement Recon", page_icon="⬗",
                       layout="wide", initial_sidebar_state="expanded")
    ui.load_css()

    with st.sidebar:
        st.markdown(
            '<div style="padding:14px 0 18px;border-bottom:1px solid var(--c-border);'
            'margin-bottom:14px;">'
            '<div style="font-size:11px;font-weight:700;letter-spacing:0.16em;'
            'color:var(--c-accent);">SETTLEMENT RECON</div>'
            '<div style="font-size:8px;letter-spacing:0.18em;color:var(--c-text3);'
            'text-transform:uppercase;margin-top:3px;">Reconciliation Controller</div>'
            '</div>', unsafe_allow_html=True)

        page = st.radio("Navigation",
                        ["Run", "Exceptions", "Audit Trail", "Accuracy", "Ask"],
                        label_visibility="collapsed")

        st.markdown('<hr class="rc-hr">', unsafe_allow_html=True)
        st.markdown('<div style="font-size:8px;letter-spacing:0.16em;'
                    'color:var(--c-text3);text-transform:uppercase;margin-bottom:6px;">'
                    'Data source</div>', unsafe_allow_html=True)
        has_keys = razorpay_client.available()
        source = st.radio(
            "Source", ["Synthetic", "Live Razorpay"],
            index=0, label_visibility="collapsed",
            disabled=not has_keys,
            help=("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env to enable"
                  if not has_keys else
                  "Live reads real orders and settled rows from your test account"),
        )
        st.session_state["source"] = "live" if source == "Live Razorpay" else "synthetic"

        if has_keys and st.session_state["source"] == "live":
            if st.button("Seed 12 test orders", use_container_width=True):
                try:
                    with st.spinner("Creating orders in Razorpay test mode…"):
                        made = razorpay_client.seed(12)
                    st.success(f"Created {len(made)} orders.")
                except razorpay_client.RazorpayError as exc:
                    st.error(str(exc))

        if st.session_state.get("result2") is not None:
            st.markdown('<hr class="rc-hr">', unsafe_allow_html=True)
            st.markdown('<div style="font-size:8px;letter-spacing:0.16em;'
                        'color:var(--c-text3);text-transform:uppercase;'
                        'margin-bottom:6px;">Viewing</div>', unsafe_allow_html=True)
            view = st.radio("Cycle", ["Cycle 1", "Cycle 2"],
                            label_visibility="collapsed", horizontal=True,
                            key="cycle_choice")
            st.session_state["view_cycle"] = 2 if view == "Cycle 2" else 1

        st.markdown('<hr class="rc-hr">', unsafe_allow_html=True)
        st.markdown('<div style="font-size:8px;letter-spacing:0.16em;'
                    'color:var(--c-text3);text-transform:uppercase;margin-bottom:6px;">'
                    'Synthetic dataset</div>', unsafe_allow_html=True)
        n_orders = st.number_input("Orders", 50, 20000, 250, step=50)
        seed = st.number_input("Seed", 1, 9999, 7)
        if st.button("Regenerate", use_container_width=True):
            from recon.generator import generate
            generate(n_orders=int(n_orders), seed=int(seed), out_dir=DATA)
            st.session_state.pop("result", None)
            st.rerun()

        # Read before the page body runs, so the badge reflects the last
        # completed run rather than lagging a rerun behind it.
        result = st.session_state.get("result")
        st.markdown('<hr class="rc-hr">', unsafe_allow_html=True)
        rzp = razorpay_client.available()
        which = qa_agent.provider()
        st.markdown(
            f'<div style="font-size:9px;color:var(--c-text3);line-height:2;'
            f'letter-spacing:0.06em;">'
            f'RAZORPAY&nbsp;<span style="color:'
            f'{"var(--c-green)" if rzp else "var(--c-text3)"};">'
            f'{"■ TEST MODE" if rzp else "□ SYNTHETIC"}</span><br>'
            f'MODEL&nbsp;&nbsp;&nbsp;&nbsp;<span style="color:'
            f'{"var(--c-green)" if which else "var(--c-text3)"};">'
            f'{"■ " + which.upper() if which else "□ LEDGER ONLY"}</span><br>'
            f'ENGINE&nbsp;&nbsp;<span style="color:var(--c-green);">■ DETERMINISTIC</span><br>'
            f'RESULT&nbsp;&nbsp;<span style="color:'
            f'{"var(--c-green)" if result else "var(--c-text3)"};">'
            f'{"■ " + format(result.match_rate, ".1f") + "% MATCHED" if result else "□ NONE"}'
            f'</span></div>',
            unsafe_allow_html=True,
        )

    truth = ensure_data(int(n_orders), int(seed))
    ui.top_bar(truth["settlement_date"], truth["settlement_utr"])

    if page == "Run":
        page_run(truth)
    elif page == "Exceptions":
        page_exceptions()
    elif page == "Audit Trail":
        page_audit()
    elif page == "Accuracy":
        page_accuracy(truth)
    else:
        page_ask()


if __name__ == "__main__":
    main()
