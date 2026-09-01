# Decisions and things that broke

Kept as I go, so the architecture walkthrough and the "what broke" question
have real answers rather than reconstructed ones.

## Why the matching is deterministic

An LLM cannot be in the matching path. The headline number of this project is
a match rate, and a match rate is meaningless if the matcher is not
reproducible. The model layer reads the match ledger and explains it; it never
produces or alters a match.

## Why the engine has no dependencies

`recon/` is standard library only. A reviewer can clone the repo and run
`python run_recon.py` with nothing installed. Streamlit and the model client
sit above the engine, never inside it.

## Why the generator writes an answer key

Accuracy can be claimed without ground truth, but not measured. `generator.py`
records every defect it plants, so `metrics.py` can report precision and recall
per defect type — including the misses.

## Bugs found while building

**Partial refunds read as overcharges.** Razorpay levies its fee on the
captured amount, but a refund row carries only the returned portion in
`amount`. Comparing the fee against that portion flagged every partial refund
as a fee mismatch. Fee precision 0.60 → 1.00.
Regression: `test_regression_partial_refund_is_not_read_as_an_overcharge`.

**Weak matching stole payments.** When two orders shared an amount, the
amount-only tier could claim a payment whose receipt named a different real
order. The robbed order was then reported as a phantom, so one bad match
produced two wrong answers. Fixed by refusing rows whose receipt names another
known order.
Regression: `test_regression_weak_tier_cannot_steal_another_orders_payment`.

**Contested rows double-counted.** Payments tied up in an ambiguity are
unmatched but not orphans — the ambiguity already names them. Sweeping them
into the orphan bucket reported the same rupees twice and dropped orphan
precision to 0.25.
Regression: `test_regression_contested_rows_are_not_also_counted_as_orphans`.

## Why the engine refuses some matches

Two orders of the same value, in the same window, with no receipt and no phone
are genuinely indistinguishable — the separating information was never
recorded. Guessing would be a coin flip presented as a match, and a wrong
pairing corrupts two rows rather than one. These are escalated as
`AMBIGUOUS_MATCH`. This is the main reason the match rate is 93% and not 100%.

## On the reported numbers

Match rate (93.2%) is the hard number: it reflects pairing under missing and
colliding identifiers. Exception classification scores 1.00 because, once a
row is paired, classification is deterministic — Razorpay labels the row type,
and the fee threshold is arithmetic. Reporting a lower figure there would be
false modesty, not honesty. The rounding-drift rows in the generator exist
specifically to prove the fee threshold does not fire on noise.

## Why the Q&A agent cannot calculate

The controller's real question is "why is Thursday's payout short", which is a
retrieval problem, not a reasoning one. Python selects the relevant ledger rows
before the model is called, and the system prompt forbids arithmetic — every
figure the model may quote is already computed and handed to it. The dashboard
shows the exact context it was given, so any answer can be checked against the
audit trail.

This is the same division the matcher keeps. If the model could compute, the
match rate would stop being reproducible, and a match rate that is not
reproducible is not a metric.

## Why credentials are optional

Both integrations degrade rather than fail. Without Razorpay keys the engine
runs on synthetic data; without an Anthropic key the same question is answered
straight from the ledger. A demo that depends on a network is a demo that can
fail in front of the person evaluating it.

The live model path is written against the documented SDK but was never
exercised — no API key was available while building. The fallback is what is
tested, and it is what runs by default. Stated in the README rather than left
for someone to discover.

## Streamlit widget state

A keyed `text_input` ignores a new `value` once the widget exists, so the
example-question buttons write into `st.session_state["q_input"]` and rerun
instead. Streamlit also paints `stNumberInputContainer` white regardless of
theme, which had to be named explicitly in the stylesheet.

## Why the model provider is swappable

The Q&A layer supports Anthropic and Gemini and uses whichever key is present.
That is possible because nothing about the design depends on which model
answers: Python does the retrieval, Python does the arithmetic, and the model
is handed a fixed set of pre-computed facts with instructions not to compute.
A provider swap changes one function call.

If the grounding lived in the prompt — "please only use the data below" — the
provider would matter a great deal, because the guarantee would rest on the
model's compliance. It does not; it rests on the model never being given the
raw rows in the first place.
