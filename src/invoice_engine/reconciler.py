"""Deterministic 3-way reconciliation (Invoice vs Purchase Order vs Goods Receipt) with 0-100 risk scoring.

Flow: ``reconcile(invoice, po, grn, approved_vendors, history)`` runs independent rule checks, each adding an
``Anomaly`` with fixed points -> ``risk_score = min(100, sum(points))`` -> verdict:
``BLOCKED`` (any critical anomaly or score >= 70), ``REVIEW`` (any other anomaly), else ``PASS``.
Same inputs always give the same output: no clocks, no randomness, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from .parser import Invoice, normalize_id

PRICE_TOLERANCE = Decimal("0.03")  # variance above 3% vs PO is flagged
MONEY_TOLERANCE = Decimal("0.01")
NEAR_DUP_DAYS = 7
BLOCK_SCORE = 70

PASS, REVIEW, BLOCKED = "PASS", "REVIEW", "BLOCKED"

# code -> (points, critical). Critical codes force BLOCKED regardless of the total score.
RULES: dict[str, tuple[int, bool]] = {
    "DUPLICATE_INVOICE_NUMBER": (60, True),
    "INVALID_IBAN": (50, True),
    "UNAPPROVED_VENDOR": (45, True),
    "ARITHMETIC_ERROR": (40, False),
    "NEAR_DUPLICATE": (35, False),
    "NO_PO": (30, False),
    "VENDOR_PO_MISMATCH": (30, False),
    "QTY_OVER_RECEIVED": (30, False),
    "PRICE_VARIANCE": (25, False),
    "QTY_OVER_PO": (25, False),
    "SKU_NOT_IN_PO": (25, False),
    "MISSING_GRN": (20, False),
    "LINE_SUM_MISMATCH": (20, False),
    "INVALID_VAT": (15, False),
}


@dataclass
class POLine:
    sku: str
    quantity: Decimal
    unit_price: Decimal


@dataclass
class PurchaseOrder:
    po_number: str
    vendor_vat: str = ""
    lines: list[POLine] = field(default_factory=list)


@dataclass
class GoodsReceipt:
    """Received quantities per SKU for one PO."""

    po_number: str
    received: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class HistoryEntry:
    """Minimal prior-invoice record used for duplicate detection (comes from the ledger)."""

    invoice_number: str
    vendor_vat: str
    issue_date: date | None
    total: Decimal


@dataclass
class Anomaly:
    code: str
    message: str
    points: int
    critical: bool


@dataclass
class ReconciliationResult:
    verdict: str
    risk_score: int
    anomalies: list[Anomaly]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "risk_score": self.risk_score,
            "anomalies": [
                {"code": a.code, "message": a.message, "points": a.points, "critical": a.critical} for a in self.anomalies
            ],
        }


def _norm_sku(s: str) -> str:
    return normalize_id(s)


def po_from_dict(d: dict[str, Any]) -> PurchaseOrder:
    """Build a PO from ``{po_number, vendor_vat, lines:[{sku, quantity, unit_price}]}``."""
    return PurchaseOrder(
        po_number=str(d["po_number"]),
        vendor_vat=str(d.get("vendor_vat", "")),
        lines=[POLine(str(ln["sku"]), Decimal(str(ln["quantity"])), Decimal(str(ln["unit_price"]))) for ln in d.get("lines", [])],
    )


def grn_from_dict(d: dict[str, Any]) -> GoodsReceipt:
    """Build a GRN from ``{po_number, lines:[{sku, quantity}]}`` (repeated SKUs are summed)."""
    received: dict[str, Decimal] = {}
    for ln in d.get("lines", []):
        k = _norm_sku(str(ln["sku"]))
        received[k] = received.get(k, Decimal("0")) + Decimal(str(ln["quantity"]))
    return GoodsReceipt(po_number=str(d["po_number"]), received=received)


def reconcile(
    invoice: Invoice,
    po: PurchaseOrder | None = None,
    grn: GoodsReceipt | None = None,
    approved_vendors: Iterable[str] | None = None,
    history: Iterable[HistoryEntry] = (),
) -> ReconciliationResult:
    """Run every rule against one invoice and return verdict, 0-100 score and explained anomalies."""
    found: list[Anomaly] = []

    def flag(code: str, message: str, points: int | None = None) -> None:
        pts, critical = RULES[code]
        found.append(Anomaly(code, message, pts if points is None else points, critical))

    vat = normalize_id(invoice.vendor_vat)

    # --- identity & payment-instruction checks
    if approved_vendors is not None and vat not in {normalize_id(v) for v in approved_vendors}:
        flag("UNAPPROVED_VENDOR", f"vendor VAT '{vat or 'missing'}' is not on the approved vendor list")
    if not invoice.iban_valid:
        flag("INVALID_IBAN", f"IBAN '{normalize_id(invoice.vendor_iban) or 'missing'}' fails ISO 13616 MOD-97 validation")
    if not invoice.vat_valid:
        flag("INVALID_VAT", f"VAT/IVA id '{vat or 'missing'}' has an invalid format")

    # --- arithmetic
    if abs(invoice.subtotal + invoice.tax - invoice.total) > MONEY_TOLERANCE:
        flag("ARITHMETIC_ERROR", f"subtotal {invoice.subtotal} + tax {invoice.tax} != total {invoice.total}")
    if invoice.lines:
        line_sum = sum((ln.line_total for ln in invoice.lines), Decimal("0"))
        if abs(line_sum - invoice.subtotal) > MONEY_TOLERANCE:
            flag("LINE_SUM_MISMATCH", f"line items sum to {line_sum} but subtotal is {invoice.subtotal}")

    # --- duplicates (same vendor only)
    for h in history:
        if normalize_id(h.vendor_vat) != vat:
            continue
        if h.invoice_number.strip().upper() == invoice.invoice_number.strip().upper():
            flag("DUPLICATE_INVOICE_NUMBER", f"invoice number {invoice.invoice_number} was already processed for this vendor")
            break
    else:
        for h in history:
            if (
                normalize_id(h.vendor_vat) == vat
                and h.issue_date and invoice.issue_date
                and abs((h.issue_date - invoice.issue_date).days) <= NEAR_DUP_DAYS
                and abs(h.total - invoice.total) <= MONEY_TOLERANCE
            ):
                flag("NEAR_DUPLICATE", f"same vendor and amount as {h.invoice_number} within {NEAR_DUP_DAYS} days")
                break

    # --- 3-way match
    if po is None:
        flag("NO_PO", f"no purchase order found for '{invoice.po_number or 'missing reference'}'")
    else:
        if po.vendor_vat and normalize_id(po.vendor_vat) != vat:
            flag("VENDOR_PO_MISMATCH", f"PO {po.po_number} was issued to a different vendor")
        po_lines = {_norm_sku(ln.sku): ln for ln in po.lines}
        if grn is None:
            flag("MISSING_GRN", f"no goods receipt recorded for PO {po.po_number}")
        for ln in invoice.lines:
            key = _norm_sku(ln.sku)
            pl = po_lines.get(key)
            if pl is None:
                flag("SKU_NOT_IN_PO", f"SKU {ln.sku} is not on PO {po.po_number}")
                continue
            if pl.unit_price and abs(ln.unit_price - pl.unit_price) / pl.unit_price > PRICE_TOLERANCE:
                pct = (ln.unit_price - pl.unit_price) / pl.unit_price * 100
                flag("PRICE_VARIANCE", f"{ln.sku} billed {ln.unit_price} vs PO {pl.unit_price} ({pct:+.1f}%, limit 3%)")
            if ln.quantity > pl.quantity:
                flag("QTY_OVER_PO", f"{ln.sku} billed {ln.quantity} but PO allows {pl.quantity}")
            if grn is not None and ln.quantity > grn.received.get(key, Decimal("0")):
                flag("QTY_OVER_RECEIVED", f"{ln.sku} billed {ln.quantity} but only {grn.received.get(key, Decimal('0'))} received")

    score = min(100, sum(a.points for a in found))
    if any(a.critical for a in found) or score >= BLOCK_SCORE:
        verdict = BLOCKED
    elif found:
        verdict = REVIEW
    else:
        verdict = PASS
    return ReconciliationResult(verdict, score, found)
