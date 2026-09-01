"""Razorpay test-mode client.

Pulls real settlement rows from the combined recon report and maps them onto
the same `Payment` shape the synthetic generator produces, so the engine is
indifferent to where a row came from.

    GET /v1/settlements/recon/combined?year=YYYY&month=MM

Auth is HTTP Basic with a test-mode key pair (`rzp_test_...`). Nothing here
moves money: the client reads settlement history, and the seeding helper only
creates orders, which is the extent of what test mode should be asked to do.

Absent credentials this module stays quiet and the caller falls back to
synthetic data. That is deliberate — the demo must not depend on a network.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import requests

from .config import ensure_loaded
from .models import Order, Payment

API_ROOT = "https://api.razorpay.com/v1"
TIMEOUT = 20


class RazorpayError(RuntimeError):
    """Raised when Razorpay answers with something other than success."""


@dataclass
class Credentials:
    key_id: str
    key_secret: str

    @property
    def is_test_mode(self) -> bool:
        return self.key_id.startswith("rzp_test_")

    @classmethod
    def from_env(cls) -> Optional["Credentials"]:
        ensure_loaded()
        key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
        key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
        if not key_id or not key_secret:
            return None
        return cls(key_id, key_secret)


def available() -> bool:
    """Whether live ingestion can be attempted at all."""
    return Credentials.from_env() is not None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _get(path: str, creds: Credentials, **params: Any) -> Any:
    resp = requests.get(
        f"{API_ROOT}{path}",
        auth=(creds.key_id, creds.key_secret),
        params={k: v for k, v in params.items() if v is not None},
        timeout=TIMEOUT,
    )
    if resp.status_code == 401:
        raise RazorpayError("Razorpay rejected the credentials (401).")
    if not resp.ok:
        raise RazorpayError(f"Razorpay returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _paise_to_rupees(v: Any) -> float:
    """Razorpay reports money in the currency's smallest unit."""
    try:
        return round(float(v or 0) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _ts(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _to_payment(row: dict) -> Payment:
    """Map one recon row onto the engine's Payment shape.

    `order_receipt` is the merchant's own order id, echoed back because it was
    supplied when the order was created. It is the primary join key, and it is
    frequently blank in real data — which is exactly the case the matching
    cascade exists to survive.
    """
    return Payment(
        payment_id=str(row.get("entity_id") or row.get("payment_id") or ""),
        order_receipt=(row.get("order_receipt") or None),
        type=str(row.get("type") or "payment"),
        amount=_paise_to_rupees(row.get("amount") or row.get("credit") or row.get("debit")),
        fee=_paise_to_rupees(row.get("fee")),
        tax=_paise_to_rupees(row.get("tax")),
        settled_at=_ts(row.get("settled_at") or row.get("created_at")),
        settlement_utr=str(row.get("settlement_utr") or ""),
        method=str(row.get("method") or ""),
        customer_phone="",   # the recon report carries no contact field
    )


def fetch_recon(year: int, month: int, creds: Optional[Credentials] = None) -> list[Payment]:
    """Fetch one month of settled rows from the combined recon report."""
    creds = creds or Credentials.from_env()
    if creds is None:
        raise RazorpayError(
            "No Razorpay credentials. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET "
            "to a test-mode pair, or run against the synthetic dataset."
        )
    body = _get("/settlements/recon/combined", creds, year=year, month=f"{month:02d}",
                count=100)
    items = body.get("items", body if isinstance(body, list) else [])
    return [_to_payment(r) for r in items]


def fetch_settlements(creds: Optional[Credentials] = None, count: int = 20) -> list[dict]:
    """Settlement headers — one per payout, carrying the UTR the bank shows."""
    creds = creds or Credentials.from_env()
    if creds is None:
        raise RazorpayError("No Razorpay credentials configured.")
    body = _get("/settlements", creds, count=count)
    return body.get("items", [])


def settlement_utrs(payments: Iterable[Payment]) -> list[str]:
    """Distinct UTRs present in a batch of rows, newest-looking first."""
    seen: dict[str, None] = {}
    for p in payments:
        if p.settlement_utr:
            seen.setdefault(p.settlement_utr, None)
    return list(seen)


# ---------------------------------------------------------------------------
# Seeding — only creates orders, never captures anything
# ---------------------------------------------------------------------------

def create_order(amount_rupees: float, receipt: str,
                 creds: Optional[Credentials] = None) -> dict:
    """Create a test-mode order carrying the merchant's own receipt id.

    Setting `receipt` at creation is what makes clean reconciliation possible
    later: Razorpay echoes it back on every settled row. Its absence in real
    merchant data is the single largest source of unmatched rows.
    """
    creds = creds or Credentials.from_env()
    if creds is None:
        raise RazorpayError("No Razorpay credentials configured.")
    if not creds.is_test_mode:
        raise RazorpayError(
            "Refusing to create orders with a live key. Use a rzp_test_ key pair."
        )
    payload = {
        "amount": int(round(amount_rupees * 100)),
        "currency": "INR",
        "receipt": receipt,
        "notes": {"source": "settlement-recon demo", "receipt": receipt},
    }
    # Test mode throttles writes hard, so back off rather than giving up: a
    # seeding helper that fails halfway leaves a confusing partial account.
    delay = 1.0
    for attempt in range(5):
        resp = requests.post(f"{API_ROOT}/orders",
                             auth=(creds.key_id, creds.key_secret),
                             json=payload, timeout=TIMEOUT)
        if resp.ok:
            return resp.json()
        if resp.status_code != 429:
            raise RazorpayError(
                f"Order creation failed ({resp.status_code}): {resp.text[:200]}")
        time.sleep(delay)
        delay *= 2
    raise RazorpayError(
        "Razorpay kept returning 429 (rate limited) after 5 attempts. "
        "Wait a minute and seed again."
    )


# ---------------------------------------------------------------------------
# Live ingestion
# ---------------------------------------------------------------------------
#
# A fresh test account has no settlement history, so the recon report comes
# back empty. Orders and payments do not — orders because you create them,
# payments once anyone has been through checkout. Reading all three and
# reporting which one supplied the data is more useful than an empty list and
# more honest than pretending settlements were available.

def fetch_orders(creds: Optional[Credentials] = None, count: int = 100) -> list[Order]:
    """Real orders, mapped onto the engine's Order shape.

    `receipt` is the merchant's own id. It is the join key the whole matching
    cascade is built around, which is why the seeding helper always sets it.
    """
    creds = creds or Credentials.from_env()
    if creds is None:
        raise RazorpayError("No Razorpay credentials configured.")
    body = _get("/orders", creds, count=count)
    out: list[Order] = []
    for row in body.get("items", []):
        receipt = (row.get("receipt") or "").strip()
        out.append(Order(
            order_id=receipt or str(row.get("id")),
            amount=_paise_to_rupees(row.get("amount")),
            created_at=_ts(row.get("created_at")),
            status="PAID" if row.get("status") == "paid" else str(row.get("status", "")).upper(),
            customer_phone="",
        ))
    return out


def fetch_payments(creds: Optional[Credentials] = None, count: int = 100) -> list[Payment]:
    """Captured payments, before they have settled.

    These carry no settlement UTR and no fee breakdown, because neither exists
    until a payout runs. The order-to-payment stage still works; the bank stage
    cannot, and the caller is told so rather than being shown a silent zero.
    """
    creds = creds or Credentials.from_env()
    if creds is None:
        raise RazorpayError("No Razorpay credentials configured.")
    body = _get("/payments", creds, count=count)
    out: list[Payment] = []
    for row in body.get("items", []):
        notes = row.get("notes") or {}
        out.append(Payment(
            payment_id=str(row.get("id")),
            order_receipt=(notes.get("receipt") or row.get("receipt") or None),
            type="payment" if row.get("status") == "captured" else str(row.get("status")),
            amount=_paise_to_rupees(row.get("amount")),
            fee=_paise_to_rupees(row.get("fee")),
            tax=_paise_to_rupees(row.get("tax")),
            settled_at=_ts(row.get("created_at")),
            settlement_utr="",          # nothing has settled yet
            method=str(row.get("method") or ""),
            customer_phone=str(row.get("contact") or "").lstrip("+").lstrip("91"),
        ))
    return out


@dataclass
class LiveData:
    orders: list[Order]
    payments: list[Payment]
    source: str            # which endpoint supplied the payment rows
    settled: bool          # False when nothing has been paid out yet
    note: str


def fetch_live(year: int, month: int,
               creds: Optional[Credentials] = None) -> LiveData:
    """Pull whatever this account actually has, best source first.

    Settlements are the right data for reconciliation but are absent on a new
    account. Payments are the next best thing and support the matching stage.
    Orders alone still say something real: these exist and nothing has settled
    against them.
    """
    creds = creds or Credentials.from_env()
    if creds is None:
        raise RazorpayError("No Razorpay credentials configured.")

    orders = fetch_orders(creds)

    try:
        settled = fetch_recon(year, month, creds)
    except RazorpayError:
        settled = []
    if settled:
        return LiveData(orders, settled, "settlements/recon/combined", True,
                        f"{len(settled)} settled rows with fees and UTRs.")

    payments = fetch_payments(creds)
    if payments:
        return LiveData(
            orders, payments, "payments", False,
            f"No settlements yet, so {len(payments)} captured payments were read "
            f"instead. These carry no UTR or fee breakdown until a payout runs, "
            f"so the bank stage is skipped and only order-to-payment matching runs."
        )

    return LiveData(
        orders, [], "orders", False,
        f"{len(orders)} orders exist and nothing has settled against them yet. "
        f"Every one is reported as awaiting settlement, which is the correct "
        f"state rather than an error."
    )


def seed(n: int = 12, creds: Optional[Credentials] = None) -> list[dict]:
    """Create a handful of test orders carrying merchant receipt ids."""
    creds = creds or Credentials.from_env()
    if creds is None:
        raise RazorpayError("No Razorpay credentials configured.")
    created = []
    for i in range(1, n + 1):
        amount = 199.0 + (i * 137) % 4200
        created.append(create_order(amount, f"ORD-{9000 + i}", creds))
        time.sleep(0.7)   # stay under the test-mode write limit
    return created
