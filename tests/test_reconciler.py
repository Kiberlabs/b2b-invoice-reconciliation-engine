from dataclasses import replace
from datetime import date
from decimal import Decimal

from invoice_engine.parser import LineItem
from invoice_engine.reconciler import BLOCKED, PASS, REVIEW, HistoryEntry, reconcile

APPROVED = ["ESB12345674"]


def codes(r):
    return {a.code for a in r.anomalies}


def test_clean_three_way_match_passes(invoice, po, grn):
    r = reconcile(invoice, po, grn, APPROVED)
    assert (r.verdict, r.risk_score, r.anomalies) == (PASS, 0, [])


def test_arithmetic_error_is_review(invoice, po, grn):
    r = reconcile(replace(invoice, total=Decimal("310.00")), po, grn, APPROVED)
    assert codes(r) == {"ARITHMETIC_ERROR"} and r.verdict == REVIEW and r.risk_score == 40


def test_duplicate_invoice_number_blocked(invoice, po, grn):
    h = [HistoryEntry("inv-1", "ESB12345674", date(2026, 10, 1), Decimal("1"))]
    r = reconcile(invoice, po, grn, APPROVED, h)
    assert "DUPLICATE_INVOICE_NUMBER" in codes(r) and r.verdict == BLOCKED


def test_near_duplicate_same_vendor_within_7_days(invoice, po, grn):
    h = [HistoryEntry("INV-0", "ESB12345674", date(2026, 11, 15), Decimal("302.50"))]
    assert codes(reconcile(invoice, po, grn, APPROVED, h)) == {"NEAR_DUPLICATE"}
    old = [HistoryEntry("INV-0", "ESB12345674", date(2026, 11, 1), Decimal("302.50"))]
    assert reconcile(invoice, po, grn, APPROVED, old).verdict == PASS
    other = [HistoryEntry("INV-0", "DE123456789", date(2026, 11, 15), Decimal("302.50"))]
    assert reconcile(invoice, po, grn, APPROVED, other).verdict == PASS


def test_price_variance_threshold_is_strictly_above_3_percent(invoice, po, grn):
    def with_price(p):
        ln = LineItem("CMP-1", "Board", Decimal("10"), Decimal(p))
        sub = ln.line_total
        return replace(invoice, lines=[ln], subtotal=sub, total=sub + invoice.tax)

    assert reconcile(with_price("25.75"), po, grn, APPROVED).verdict == PASS  # exactly +3.0%
    r = reconcile(with_price("25.80"), po, grn, APPROVED)  # +3.2%
    assert "PRICE_VARIANCE" in codes(r) and r.verdict == REVIEW


def test_invalid_iban_blocked(invoice, po, grn):
    r = reconcile(replace(invoice, vendor_iban="ES9121000418450200051333"), po, grn, APPROVED)
    assert codes(r) == {"INVALID_IBAN"} and r.verdict == BLOCKED


def test_unapproved_vendor_blocked(invoice, po, grn):
    assert "UNAPPROVED_VENDOR" in codes(reconcile(invoice, po, grn, ["DE123456789"]))
    assert reconcile(invoice, po, grn, ["DE123456789"]).verdict == BLOCKED


def test_quantity_over_received_and_missing_grn(invoice, po, grn):
    short = replace(grn, received={"CMP1": Decimal("6")})
    assert codes(reconcile(invoice, po, short, APPROVED)) == {"QTY_OVER_RECEIVED"}
    assert codes(reconcile(invoice, po, None, APPROVED)) == {"MISSING_GRN"}


def test_no_po_and_vendor_mismatch(invoice, po, grn):
    assert "NO_PO" in codes(reconcile(invoice, None, None, APPROVED))
    bad_po = replace(po, vendor_vat="DE123456789")
    assert "VENDOR_PO_MISMATCH" in codes(reconcile(invoice, bad_po, grn, APPROVED))


def test_score_capped_at_100_and_deterministic(invoice, po, grn):
    worst = replace(invoice, vendor_iban="", vendor_vat="", total=Decimal("999"))
    a, b = reconcile(worst, None, None, APPROVED), reconcile(worst, None, None, APPROVED)
    assert a.risk_score == 100 and a.to_dict() == b.to_dict() and a.verdict == BLOCKED
