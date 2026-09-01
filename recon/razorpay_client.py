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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import requests

from .models import Payment

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
    resp = requests.post(
        f"{API_ROOT}/orders",
        auth=(creds.key_id, creds.key_secret),
        json={
            "amount": int(round(amount_rupees * 100)),
            "currency": "INR",
            "receipt": receipt,
            "notes": {"source": "settlement-recon demo"},
        },
        timeout=TIMEOUT,
    )
    if not resp.ok:
        raise RazorpayError(f"Order creation failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()
