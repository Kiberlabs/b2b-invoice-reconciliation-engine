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
import re
from typing import Any, Iterable
import unicodedata

from .parser import Invoice, normalize_id

_ZW_RE = re.compile(r"[\u200b-\u200d\u2060\ufeff]")


def _norm_inv_num(value: str) -> str:
    return unicodedata.normalize("NFKC", _ZW_RE.sub("", value)).strip().upper()

PRICE_TOLERANCE = Decimal("0.03")  # variance above 3% vs PO is flagged
MONEY_TOLERANCE = Decimal("0.01")
NEAR_DUP_DAYS = 7
BLOCK_SCORE = 70

PASS, REVIEW, BLOCKED = "PASS", "REVIEW", "BLOCKED"

# code -> (points, critical). Critical codes force BLOCKED regardless of the total score.
RULES: dict[str, tuple[int, bool]] = {
    "NEGATIVE_AMOUNT": (70, True),
    "DUPLICATE_INVOICE_NUMBER": (60, True),
    "INVALID_IBAN": (50, True),
    "UNAPPROVED_VENDOR": (45, True),
    "ARITHMETIC_ERROR": (40, False),
    "CURRENCY_MISMATCH": (35, False),
    "NEAR_DUPLICATE": (35, False),
    "NO_PO": (30, False),
    "PO_NUMBER_MISMATCH": (30, False),
    "GRN_PO_MISMATCH": (30, False),
    "VENDOR_PO_MISMATCH": (30, False),
    "QTY_OVER_RECEIVED": (30, False),
    "PRICE_VARIANCE": (25, False),
    "QTY_OVER_PO": (25, False),
    "SKU_NOT_IN_PO": (25, False),
    "EMPTY_INVOICE_LINES": (25, False),
    "MISSING_GRN": (20, False),
    "LINE_SUM_MISMATCH": (20, False),
    "INVALID_VAT": (15, False),
}


@dataclass
class POLine:
    sku: str
    quantity: Decimal
    unit_price: Decimal

    def __post_init__(self) -> None:
        if not (self.quantity.is_finite() and self.unit_price.is_finite()):
            raise ValueError("PO line quantity and unit_price must be finite")
        if self.quantity < 0 or self.unit_price < 0:
            raise ValueError("PO line quantity and unit_price must be non-negative")


@dataclass
class PurchaseOrder:
    po_number: str
    vendor_vat: str = ""
    lines: list[POLine] = field(default_factory=list)
    currency: str = ""


@dataclass
class GoodsReceipt:
    """Received quantities per SKU for one PO."""

    po_number: str
    received: dict[str, Decimal] = field(default_factory=dict)
    currency: str = ""


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
        currency=str(d.get("currency") or "").strip().upper(),
    )


def grn_from_dict(d: dict[str, Any]) -> GoodsReceipt:
    """Build a GRN from ``{po_number, lines:[{sku, quantity}]}`` (repeated SKUs are summed)."""
    received: dict[str, Decimal] = {}
    for ln in d.get("lines", []):
        k = _norm_sku(str(ln["sku"]))
        q = Decimal(str(ln["quantity"]))
        if not q.is_finite() or q < 0:
            raise ValueError("GRN line quantity must be finite and non-negative")
        received[k] = received.get(k, Decimal("0")) + q
    return GoodsReceipt(
        po_number=str(d["po_number"]),
        received=received,
        currency=str(d.get("currency") or "").strip().upper(),
    )


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
    if invoice.total < 0 or invoice.subtotal < 0 or invoice.tax < 0:
        flag("NEGATIVE_AMOUNT", f"negative invoice amount (subtotal={invoice.subtotal}, tax={invoice.tax}, total={invoice.total})")
    if abs(invoice.subtotal + invoice.tax - invoice.total) > MONEY_TOLERANCE:
        flag("ARITHMETIC_ERROR", f"subtotal {invoice.subtotal} + tax {invoice.tax} != total {invoice.total}")
    if invoice.lines:
        line_sum = sum((ln.line_total for ln in invoice.lines), Decimal("0"))
        if abs(line_sum - invoice.subtotal) > MONEY_TOLERANCE:
            flag("LINE_SUM_MISMATCH", f"line items sum to {line_sum} but subtotal is {invoice.subtotal}")

    # --- currency consistency across legs that specify one
    inv_cur = (invoice.currency or "").strip().upper()
    po_cur = (po.currency or "").strip().upper() if po is not None else ""
    grn_cur = (grn.currency or "").strip().upper() if grn is not None else ""
    if len({c for c in (inv_cur, po_cur, grn_cur) if c}) > 1:
        flag("CURRENCY_MISMATCH", f"currency mismatch across documents (invoice={inv_cur or 'unset'}, po={po_cur or 'unset'}, grn={grn_cur or 'unset'})")

    # --- duplicates (same vendor only; empty VAT cannot identify a vendor)
    if vat:
        for h in history:
            if normalize_id(h.vendor_vat) != vat:
                continue
            if _norm_inv_num(h.invoice_number) == _norm_inv_num(invoice.invoice_number):
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
        if normalize_id(invoice.po_number) != normalize_id(po.po_number):
            flag("PO_NUMBER_MISMATCH", f"invoice PO '{invoice.po_number or 'missing'}' does not match PO {po.po_number}")
        if po.vendor_vat and normalize_id(po.vendor_vat) != vat:
            flag("VENDOR_PO_MISMATCH", f"PO {po.po_number} was issued to a different vendor")

        po_qty: dict[str, Decimal] = {}
        po_prices: dict[str, set[Decimal]] = {}
        po_cost: dict[str, Decimal] = {}
        for pl in po.lines:
            key = _norm_sku(pl.sku)
            po_qty[key] = po_qty.get(key, Decimal("0")) + pl.quantity
            po_prices.setdefault(key, set()).add(pl.unit_price)
            po_cost[key] = po_cost.get(key, Decimal("0")) + pl.quantity * pl.unit_price
        po_price: dict[str, Decimal] = {
            key: (next(iter(prices)) if len(prices) == 1 else (po_cost[key] / po_qty[key] if po_qty[key] > 0 else next(iter(prices))))
            for key, prices in po_prices.items()
        }

        grn_received: dict[str, Decimal] | None = None
        if grn is None:
            flag("MISSING_GRN", f"no goods receipt recorded for PO {po.po_number}")
        else:
            if grn.po_number.strip() and po.po_number.strip() and normalize_id(grn.po_number) != normalize_id(po.po_number):
                flag("GRN_PO_MISMATCH", f"GRN PO '{grn.po_number}' does not match PO {po.po_number}")
            grn_received = {}
            for raw_k, q in grn.received.items():
                if not q.is_finite() or q < 0:
                    raise ValueError("GRN received quantity must be finite and non-negative")
                nk = _norm_sku(raw_k)
                grn_received[nk] = grn_received.get(nk, Decimal("0")) + q

        if po_qty and not invoice.lines:
            flag("EMPTY_INVOICE_LINES", f"invoice has no line items for PO {po.po_number}")

        inv_qty: dict[str, Decimal] = {}
        inv_label: dict[str, str] = {}
        missing_skus: set[str] = set()
        price_flagged: set[str] = set()
        for ln in invoice.lines:
            key = _norm_sku(ln.sku)
            inv_qty[key] = inv_qty.get(key, Decimal("0")) + ln.quantity
            inv_label.setdefault(key, ln.sku)
            if key not in po_qty:
                if key not in missing_skus:
                    missing_skus.add(key)
                    flag("SKU_NOT_IN_PO", f"SKU {ln.sku} is not on PO {po.po_number}")
                continue
            if key not in price_flagged:
                unit_p = po_price[key]
                if unit_p == 0:
                    if ln.unit_price != 0:
                        price_flagged.add(key)
                        flag("PRICE_VARIANCE", f"{ln.sku} billed {ln.unit_price} vs PO {unit_p} (non-zero on zero-priced PO)")
                elif abs(ln.unit_price - unit_p) / unit_p > PRICE_TOLERANCE:
                    price_flagged.add(key)
                    pct = (ln.unit_price - unit_p) / unit_p * 100
                    flag("PRICE_VARIANCE", f"{ln.sku} billed {ln.unit_price} vs PO {unit_p} ({pct:+.1f}%, limit 3%)")

        for key, qty in inv_qty.items():
            if key not in po_qty:
                continue
            label = inv_label[key]
            if qty > po_qty[key]:
                flag("QTY_OVER_PO", f"{label} billed {qty} across invoice lines but PO allows {po_qty[key]}")
            if grn_received is not None and qty > grn_received.get(key, Decimal("0")):
                flag("QTY_OVER_RECEIVED", f"{label} billed {qty} across invoice lines but only {grn_received.get(key, Decimal('0'))} received")

    score = min(100, sum(a.points for a in found))
    if any(a.critical for a in found) or score >= BLOCK_SCORE:
        verdict = BLOCKED
    elif found:
        verdict = REVIEW
    else:
        verdict = PASS
    return ReconciliationResult(verdict, score, found)
