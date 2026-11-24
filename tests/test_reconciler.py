from dataclasses import replace
from datetime import date
from decimal import Decimal
import random

from invoice_engine.parser import Invoice, LineItem
from invoice_engine.reconciler import (
    BLOCKED,
    BLOCK_SCORE,
    PASS,
    REVIEW,
    GoodsReceipt,
    HistoryEntry,
    POLine,
    PurchaseOrder,
    grn_from_dict,
    po_from_dict,
    reconcile,
)

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


def test_split_line_qty_bypass_caught(invoice, po, grn):
    split_lines = [
        LineItem("CMP-1", "Board A", Decimal("6"), Decimal("25.00")),
        LineItem("CMP-1", "Board B", Decimal("6"), Decimal("25.00")),
    ]
    split_inv = replace(invoice, lines=split_lines, subtotal=Decimal("300.00"), tax=Decimal("63.00"), total=Decimal("363.00"))
    r = reconcile(split_inv, po, grn, APPROVED)
    assert codes(r) == {"QTY_OVER_PO", "QTY_OVER_RECEIVED"}
    assert r.verdict == REVIEW and r.risk_score == 55


def test_duplicate_po_sku_lines_summed(invoice, grn):
    dup_po = PurchaseOrder(
        po_number="PO-1",
        vendor_vat="ESB12345674",
        lines=[
            POLine("CMP-1", Decimal("5"), Decimal("25.00")),
            POLine("cmp1", Decimal("5"), Decimal("25.00")),
        ],
    )
    r = reconcile(invoice, dup_po, grn, APPROVED)
    assert (r.verdict, r.risk_score, r.anomalies) == (PASS, 0, [])


def test_po_number_mismatch_and_empty_invoice_lines(invoice, po, grn):
    mismatch = reconcile(replace(invoice, po_number="PO-999"), po, grn, APPROVED)
    assert "PO_NUMBER_MISMATCH" in codes(mismatch)

    empty_lines = reconcile(replace(invoice, lines=[]), po, grn, APPROVED)
    assert "EMPTY_INVOICE_LINES" in codes(empty_lines)


def test_empty_vat_does_not_collide_across_vendors(po, grn):
    inv_empty_vat = Invoice(
        invoice_number="INV-1",
        vendor_name="No VAT Corp",
        vendor_vat="",
        vendor_iban="ES9121000418450200051332",
        issue_date=date(2026, 11, 20),
        po_number="PO-1",
        lines=[LineItem("CMP-1", "Board", Decimal("10"), Decimal("25.00"))],
        subtotal=Decimal("250.00"),
        tax=Decimal("52.50"),
        total=Decimal("302.50"),
    )
    h = [HistoryEntry("INV-1", "", date(2026, 11, 20), Decimal("302.50"))]
    r = reconcile(inv_empty_vat, replace(po, vendor_vat=""), grn, APPROVED, h)
    assert "DUPLICATE_INVOICE_NUMBER" not in codes(r)
    assert "NEAR_DUPLICATE" not in codes(r)
    assert codes(r) == {"UNAPPROVED_VENDOR", "INVALID_VAT"}


def test_negative_invoice_totals_always_blocked_even_with_empty_lines(invoice, po, grn):
    empty_po = PurchaseOrder("PO-1", "ESB12345674", [])
    neg_empty = replace(invoice, lines=[], subtotal=Decimal("-100.00"), tax=Decimal("0"), total=Decimal("-100.00"))
    r = reconcile(neg_empty, empty_po, grn, APPROVED)
    assert r.verdict == BLOCKED and "NEGATIVE_AMOUNT" in codes(r) and r.risk_score >= BLOCK_SCORE

    neg_tax = replace(invoice, tax=Decimal("-1.00"), total=Decimal("249.00"))
    r2 = reconcile(neg_tax, po, grn, APPROVED)
    assert r2.verdict == BLOCKED and "NEGATIVE_AMOUNT" in codes(r2)


def test_grn_po_mismatch_flagged(invoice, po, grn):
    bad_grn = replace(grn, po_number="PO-999")
    r = reconcile(invoice, po, bad_grn, APPROVED)
    assert codes(r) == {"GRN_PO_MISMATCH"} and r.verdict == REVIEW and r.risk_score == 30


def test_currency_mismatch_across_invoice_po_grn(invoice, po, grn):
    po_usd = po_from_dict({"po_number": "PO-1", "vendor_vat": "ESB12345674", "currency": "usd", "lines": [{"sku": "CMP-1", "quantity": 10, "unit_price": "25.00"}]})
    r1 = reconcile(invoice, po_usd, grn, APPROVED)
    assert codes(r1) == {"CURRENCY_MISMATCH"} and r1.verdict == REVIEW and r1.risk_score == 35

    grn_gbp = grn_from_dict({"po_number": "PO-1", "currency": "GBP", "lines": [{"sku": "CMP-1", "quantity": 10}]})
    r2 = reconcile(invoice, po, grn_gbp, APPROVED)
    assert codes(r2) == {"CURRENCY_MISMATCH"} and r2.verdict == REVIEW

    po_eur = replace(po, currency="eur")
    grn_eur = replace(grn, currency="EUR")
    assert reconcile(replace(invoice, currency="eur"), po_eur, grn_eur, APPROVED).verdict == PASS


def test_deterministic_seeded_reconciler_fuzzing():
    rng = random.Random(20261123)
    vats = ["ESB12345674", "DE123456789", "BAD", ""]
    ibans = ["ES9121000418450200051332", "DE89370400440532013000", "BADIBAN", ""]
    curs = ["EUR", "USD", "GBP", ""]

    for i in range(150):
        qty = Decimal(str(rng.choice([0, 1, 5, 10, 15])))
        price = Decimal(str(rng.choice(["0.00", "25.00", "26.50", "99.00"])))
        lines = [LineItem("CMP-1", "Board", qty, price)] if rng.random() > 0.2 else []
        sub = Decimal(str(rng.choice(["-100.00", "0.00", "250.00", "300.00"])))
        tax = Decimal(str(rng.choice(["-5.00", "0.00", "52.50"])))
        tot = sub + tax if rng.random() > 0.3 else Decimal("999.00")
        inv = Invoice(
            invoice_number=f"INV-{i % 10}",
            vendor_vat=rng.choice(vats),
            vendor_iban=rng.choice(ibans),
            issue_date=date(2026, 11, rng.randint(1, 28)),
            currency=rng.choice(["EUR", "USD", "GBP"]),
            po_number=rng.choice(["PO-1", "PO-2", ""]),
            lines=lines,
            subtotal=sub,
            tax=tax,
            total=tot,
        )
        po_obj = None if rng.random() < 0.2 else PurchaseOrder(
            po_number=rng.choice(["PO-1", "PO-2"]),
            vendor_vat=rng.choice(vats),
            lines=[POLine("CMP-1", Decimal("10"), Decimal("25.00"))],
            currency=rng.choice(curs),
        )
        grn_obj = None if rng.random() < 0.2 else GoodsReceipt(
            po_number=rng.choice(["PO-1", "PO-2"]),
            received={"CMP1": Decimal(str(rng.choice([0, 5, 10])))},
            currency=rng.choice(curs),
        )
        hist = [HistoryEntry(f"INV-{rng.randint(0, 9)}", rng.choice(vats), date(2026, 11, 18), tot)]
        r1 = reconcile(inv, po_obj, grn_obj, APPROVED, hist)
        r2 = reconcile(inv, po_obj, grn_obj, APPROVED, hist)
        assert r1.to_dict() == r2.to_dict()
        assert 0 <= r1.risk_score <= 100
        expected_verdict = BLOCKED if (any(a.critical for a in r1.anomalies) or r1.risk_score >= BLOCK_SCORE) else (REVIEW if r1.anomalies else PASS)
        assert r1.verdict == expected_verdict
        if inv.total < 0 or inv.subtotal < 0 or inv.tax < 0:
            assert r1.verdict == BLOCKED and any(a.code == "NEGATIVE_AMOUNT" for a in r1.anomalies)
