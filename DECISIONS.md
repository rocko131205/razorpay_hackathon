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

The Gemini path has since been exercised against a real key — the truncation
and model-discovery fixes recorded below came out of running it, not from
reading docs. The Anthropic path is written against the documented SDK and has
not been run. The ledger-only fallback is what runs by default and is what the
tests cover. Stated in the README rather than left for someone to discover.

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

## What the live Razorpay path actually showed

Verified against a real test account. All three endpoints authenticate, and all
three return zero rows on a fresh account — which is why the fallback exists.
After seeding, `/v1/orders` returns real orders and the engine reports all 19 as
awaiting settlement. That is the correct answer, not a failure.

Two things surfaced only by running it:

**Test mode rate-limits writes.** Creating twelve orders in a loop returned 429
partway through, leaving a half-seeded account. The seeder now paces itself and
retries with backoff.

**A zero match rate read as a failure.** With nothing settled there is nothing
to match, so the live view reports a count of orders awaiting settlement rather
than 0%. A metric that cannot yet be computed should not be displayed as a bad
score.

## Gemini 2.5 spends output budget on reasoning

The first live answer stopped mid-list. `maxOutputTokens: 2048` was being
consumed partly by the model's own reasoning, so the visible answer truncated
rather than erroring. Raised to 8192.

Model discovery also earned its keep: `ListModels` resolved `gemini-2.5-flash`,
where the hardcoded default would have named an older model.

## Cycle two re-flagged everything that had already settled

Re-running against the next payout compared every row to that payout's UTR, so
the 236 rows that had settled correctly in cycle one came back as 189
`LATE_SETTLEMENT` findings — 281 exceptions and ₹419,287 at risk, burying the
22 findings that had actually changed. A row carrying a payout that has already
landed is settled, not late, so `reconcile` now takes `prior_utrs`.

Cycle two: 281 exceptions → 70, ₹419,287 → ₹63,311, `LATE_SETTLEMENT` 189 → 0.
The resolution numbers were unaffected, which is the point — they were reading
`UNRESOLVED_NO_ROW`, not the noise around it.

Scoring needed the same correction. Cycle one expects the late orders to be
open; by cycle two they should be closed, so `score()` takes the cycle and
expects only the genuine phantoms. Grading cycle two against cycle one's
expectation counted every correct resolution as a miss.

## Every screen but one was stuck on cycle one

Cycle two is stored alongside cycle one rather than replacing it, because the
resolution table needs both. The consequence went unnoticed: the queue, the
audit trail, the accuracy page and the Q&A all read the first result and had no
way to see the second. The loop closed in one table and nowhere else. A sidebar
switch now selects which cycle every page reads, with briefs and answers cached
per cycle so switching does not silently show stale text.

## Retrieval could not see what a question was about

"How much am I being overcharged in fees?" returned *the facts do not specify*.
That was the grounding working exactly as designed — and the retrieval failing.
`select_context` knew two modes, an explicit id or the largest amounts by value,
and fee mismatches are small, so they never survived the top-40 cut. The model
was never shown them and correctly declined to invent a total.

Retrieval now recognises the subject of a question — fee, chargeback, refund,
duplicate, orphan, split, phantom — and per-type totals are always supplied
pre-computed. The same question now answers ₹143.95 across 12 exceptions, which
matches the ledger exactly.

The pool is also ordered by severity before amount, as the exception queue is.
Sorting by amount alone had the model naming the largest total as the top
priority while the queue on screen led with the chargebacks.

## Reasoning budget on a call that does not reason

"Why is the payout short this cycle?" took 27.9s and returned 272 words listing
every exception type. Gemini 2.5 was spending most of that budget on reasoning
about a question where Python had already done the selection and the
arithmetic. With `thinkingBudget: 0` and brevity as a hard constraint rather
than a closing suggestion: 2.8s, 52 words, same figures.

Twenty-eight seconds of dead air is not a latency problem, it is a demo that
fails in front of the person evaluating it.
