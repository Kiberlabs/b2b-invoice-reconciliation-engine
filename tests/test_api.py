from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
import io
from pathlib import Path
import sqlite3
import tempfile
import threading

import pytest
from fastapi.testclient import TestClient

from invoice_engine.api import create_app

KEY = "test-key-123"
H = {"X-API-Key": KEY}
PO = {"po_number": "PO-1", "vendor_vat": "ESB12345674", "lines": [{"sku": "CMP-1", "quantity": 10, "unit_price": "25.00"}]}
GRN = {"po_number": "PO-1", "lines": [{"sku": "CMP-1", "quantity": 10}]}


def inv(number="INV-1", **kw):
    d = {"invoice_number": number, "vendor": {"name": "Northwind", "vat": "ESB12345674", "iban": "ES9121000418450200051332"},
         "issue_date": "2026-11-20", "po_number": "PO-1",
         "lines": [{"sku": "CMP-1", "quantity": 10, "unit_price": "25.00"}], "subtotal": "250.00", "tax": "52.50", "total": "302.50"}
    d.update(kw)
    return d


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "api.db"), KEY))


def post(client, invoice, **kw):
    return client.post("/v1/reconcile", headers=H, json={"invoice": invoice, "po": PO, "grn": GRN, **kw})


def test_health_is_public(client):
    assert client.get("/health").json()["status"] == "ok"


def test_dashboard_200_html_with_csp(client):
    r = client.get("/dashboard")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "Invoice Reconciliation" in r.text and "Content-Security-Policy" in r.headers


@pytest.mark.parametrize("path", ["/v1/ledger", "/v1/ledger/verify", "/v1/export.csv"])
def test_v1_requires_key(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get(path, headers=H).status_code == 200


def test_reconcile_requires_key(client):
    assert client.post("/v1/reconcile", json={"invoice": inv()}).status_code == 401


def test_app_refuses_to_start_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("INVOICE_ENGINE_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        create_app(str(tmp_path / "x.db"))


def test_reconcile_pass_then_duplicate_blocked(client):
    first = post(client, inv()).json()
    assert first["result"]["verdict"] == "PASS" and first["ledger"]["seq"] == 1
    second = post(client, inv()).json()
    assert second["result"]["verdict"] == "BLOCKED"
    assert any(a["code"] == "DUPLICATE_INVOICE_NUMBER" for a in second["result"]["anomalies"])


def test_reconcile_flags_bad_iban_and_arithmetic(client):
    bad = inv("INV-2", total="999.00")
    bad["vendor"]["iban"] = "ES9121000418450200051333"
    codes = {a["code"] for a in post(client, bad).json()["result"]["anomalies"]}
    assert {"INVALID_IBAN", "ARITHMETIC_ERROR"} <= codes


def test_reconcile_accepts_ocr_text_and_xml(client):
    text = "Invoice No: T-1\nVAT: ESB12345674\nIBAN: ES9121000418450200051332\nDate: 2026-11-20\nPO: PO-1\n" \
           "CMP-1 Board 10 x 25,00\nSubtotal: 250,00\nIVA 21%: 52,50\nTotal: 302,50"
    r = post(client, text, format="text")
    assert r.status_code == 200 and r.json()["result"]["verdict"] == "PASS"


def test_reconcile_bad_payloads_422(client):
    assert post(client, "garbage without fields").status_code == 422
    assert client.post("/v1/reconcile", headers=H, json={"invoice": inv("X"), "po": {"nope": 1}}).status_code == 422


def test_ledger_verify_detects_tampering_over_api(client, tmp_path):
    post(client, inv("A")); post(client, inv("B"))
    assert client.get("/v1/ledger/verify", headers=H).json()["ok"] is True
    db = sqlite3.connect(tmp_path / "api.db")
    db.execute("UPDATE audit_entries SET verdict='BLOCKED', risk_score=99 WHERE seq=1")
    db.commit(); db.close()
    body = client.get("/v1/ledger/verify", headers=H).json()
    assert body["ok"] is False and body["broken_at"] == 1


def test_ledger_search_endpoint(client):
    post(client, inv("FIND-ME")); post(client, inv("OTHER"))
    rows = client.get("/v1/ledger", headers=H, params={"q": "FIND"}).json()
    assert [r["invoice_number"] for r in rows] == ["FIND-ME"]


def test_csv_export_neutralises_formulas(client):
    post(client, inv("=HYPERLINK(1)"))
    r = client.get("/v1/export.csv", headers=H)
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[0]["invoice_number"] == "'=HYPERLINK(1)" and rows[0]["entry_hash"]


def test_approved_vendors_empty_env_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICE_ENGINE_APPROVED_VENDORS", "")
    c = TestClient(create_app(str(tmp_path / "empty_vendors.db"), KEY))
    r = post(c, inv("VEND-1"))
    assert r.status_code == 200
    body = r.json()["result"]
    assert body["verdict"] == "BLOCKED"
    assert any(a["code"] == "UNAPPROVED_VENDOR" for a in body["anomalies"])


def test_whitespace_api_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICE_ENGINE_API_KEY", "   ")
    with pytest.raises(RuntimeError):
        create_app(str(tmp_path / "ws.db"))


@pytest.mark.parametrize(
    "bad_extra",
    [
        {"po": {"po_number": "PO-1", "vendor_vat": "ESB12345674", "lines": [{"sku": "CMP-1", "quantity": -1, "unit_price": "25.00"}]}},
        {"po": {"po_number": "PO-1", "vendor_vat": "ESB12345674", "lines": [{"sku": "CMP-1", "quantity": "NaN", "unit_price": "25.00"}]}},
        {"grn": {"po_number": "PO-1", "lines": [{"sku": "CMP-1", "quantity": -2}]}},
        {"grn": {"po_number": "PO-1", "lines": [{"sku": "CMP-1", "quantity": "Infinity"}]}},
    ],
)
def test_bad_po_grn_negative_or_nan_returns_422(client, bad_extra):
    payload = {"invoice": inv("BAD-POGRN"), **bad_extra}
    r = client.post("/v1/reconcile", headers=H, json=payload)
    assert r.status_code == 422


def test_export_csv_exceeds_5000_rows(tmp_path):
    app = create_app(str(tmp_path / "big_export.db"), KEY)
    ledger = app.state.ledger
    inv_row = {
        "invoice_number": "INV-BULK",
        "vendor_vat": "ESB12345674",
        "vendor_name": "Northwind",
        "issue_date": "2026-11-20",
        "currency": "EUR",
        "total": "302.50",
    }
    res_row = {"verdict": "PASS", "risk_score": 0, "anomalies": []}
    with ledger._use() as db:
        # Insert 5050 hash-valid rows in a single transaction for fast streaming test
        from invoice_engine.ledger import GENESIS_HASH, _entry_hash
        prev = GENESIS_HASH
        db.execute("BEGIN IMMEDIATE")
        for seq in range(1, 5051):
            row = {
                "seq": seq,
                "ts": "2026-11-20T00:00:00+00:00",
                "invoice_number": f"BULK-{seq}",
                "vendor_vat": inv_row["vendor_vat"],
                "vendor_name": inv_row["vendor_name"],
                "issue_date": inv_row["issue_date"],
                "currency": inv_row["currency"],
                "total": inv_row["total"],
                "verdict": res_row["verdict"],
                "risk_score": res_row["risk_score"],
                "anomalies": "[]",
            }
            row["prev_hash"] = prev
            row["entry_hash"] = _entry_hash(prev, row)
            prev = row["entry_hash"]
            db.execute(
                "INSERT INTO audit_entries (seq, ts, invoice_number, vendor_vat, vendor_name, issue_date, currency, total, verdict, risk_score, anomalies, prev_hash, entry_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [row[k] for k in ("seq", "ts", "invoice_number", "vendor_vat", "vendor_name", "issue_date", "currency", "total", "verdict", "risk_score", "anomalies", "prev_hash", "entry_hash")],
            )
        db.execute("COMMIT")

    c = TestClient(app)
    r = c.get("/v1/export.csv", headers=H)
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 5050
    assert rows[0]["seq"] == "1" and rows[-1]["seq"] == "5050"
    assert rows[-1]["invoice_number"] == "BULK-5050"


def test_csv_export_sanitizes_currency_and_whitespace_newline_formulas(client):
    def vendor_named(name: str) -> dict:
        return {"name": name, "vat": "ESB12345674", "iban": "ES9121000418450200051332"}

    payloads = [
        inv("CSV-1", currency="=HYPERLINK(1)", vendor=vendor_named(" =1+1")),
        inv("CSV-2", vendor=vendor_named("\n=2+2")),
        inv("CSV-3", vendor=vendor_named("\r+3+3")),
        inv("CSV-4", vendor=vendor_named("Acme\n=4+4")),
    ]
    for p in payloads:
        assert post(client, p).status_code == 200

    r = client.get("/v1/export.csv", headers=H)
    assert r.status_code == 200
    rows = {row["invoice_number"]: row for row in csv.DictReader(io.StringIO(r.text))}
    assert rows["CSV-1"]["currency"] == "'=HYPERLINK(1)"
    assert rows["CSV-1"]["vendor_name"] == "' =1+1"
    assert rows["CSV-2"]["vendor_name"] == "' =2+2"
    assert rows["CSV-3"]["vendor_name"] == "' +3+3"
    assert rows["CSV-4"]["vendor_name"] == "Acme =4+4"


def test_reconcile_negative_json_or_xml_returns_422(client):
    assert post(client, inv("NEG-JSON", total="-302.50")).status_code == 422
    neg_xml = "<Invoice><ID>NEG-XML</ID><LegalMonetaryTotal><PayableAmount>-100.00</PayableAmount></LegalMonetaryTotal></Invoice>"
    assert post(client, neg_xml, format="xml").status_code == 422


def test_concurrent_duplicate_race_is_atomic_and_cleans_up():
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(str(Path(tmp) / "race.db"), KEY)
        c = TestClient(app)
        barrier = threading.Barrier(8)

        def fire(_: int) -> dict:
            barrier.wait(timeout=5.0)
            resp = post(c, inv("RACE-DUP-1"))
            assert resp.status_code == 200
            return resp.json()

        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(fire, range(8)))

            verdicts = [r["result"]["verdict"] for r in results]
            assert verdicts.count("PASS") == 1
            assert verdicts.count("BLOCKED") == 7
            blocked_codes = [
                {a["code"] for a in r["result"]["anomalies"]}
                for r in results
                if r["result"]["verdict"] == "BLOCKED"
            ]
            assert all("DUPLICATE_INVOICE_NUMBER" in cset for cset in blocked_codes)

            verify = c.get("/v1/ledger/verify", headers=H).json()
            assert verify["ok"] is True and verify["entries"] == 8
        finally:
            app.state.ledger.close()


def test_concurrent_near_duplicate_race_is_atomic_and_cleans_up():
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(str(Path(tmp) / "near_race.db"), KEY)
        c = TestClient(app)
        barrier = threading.Barrier(4)

        def fire(idx: int) -> dict:
            barrier.wait(timeout=5.0)
            resp = post(c, inv(f"ND-{idx}", issue_date=f"2026-11-{18 + idx:02d}"))
            assert resp.status_code == 200
            return resp.json()

        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(fire, range(4)))

            verdicts = [r["result"]["verdict"] for r in results]
            assert verdicts.count("PASS") == 1
            assert verdicts.count("REVIEW") == 3
            review_codes = [
                {a["code"] for a in r["result"]["anomalies"]}
                for r in results
                if r["result"]["verdict"] == "REVIEW"
            ]
            assert all("NEAR_DUPLICATE" in cset for cset in review_codes)

            verify = c.get("/v1/ledger/verify", headers=H).json()
            assert verify["ok"] is True and verify["entries"] == 4
        finally:
            app.state.ledger.close()


def test_csv_export_all_six_formula_prefixes(client):
    payloads = [
        inv("CSV-EQ", vendor={"name": "=1+1", "vat": "ESB12345674", "iban": "ES9121000418450200051332"}),
        inv("CSV-PL", vendor={"name": "+1+1", "vat": "ESB12345674", "iban": "ES9121000418450200051332"}),
        inv("CSV-MN", vendor={"name": "-1+1", "vat": "ESB12345674", "iban": "ES9121000418450200051332"}),
        inv("CSV-AT", vendor={"name": "@SUM(1,1)", "vat": "ESB12345674", "iban": "ES9121000418450200051332"}),
        inv("CSV-TB", vendor={"name": "\t=1+1", "vat": "ESB12345674", "iban": "ES9121000418450200051332"}),
        inv("CSV-CR", vendor={"name": "\r=1+1", "vat": "ESB12345674", "iban": "ES9121000418450200051332"}),
    ]
    for p in payloads:
        assert post(client, p).status_code == 200

    r = client.get("/v1/export.csv", headers=H)
    assert r.status_code == 200
    rows = {row["invoice_number"]: row for row in csv.DictReader(io.StringIO(r.text))}
    assert rows["CSV-EQ"]["vendor_name"] == "'=1+1"
    assert rows["CSV-PL"]["vendor_name"] == "'+1+1"
    assert rows["CSV-MN"]["vendor_name"] == "'-1+1"
    assert rows["CSV-AT"]["vendor_name"] == "'@SUM(1,1)"
    assert rows["CSV-TB"]["vendor_name"] == "'\t=1+1"
    assert rows["CSV-CR"]["vendor_name"] == "' =1+1"


def test_unicode_homoglyph_zero_width_and_nfd_nfkc_boundaries(client):
    first = post(client, inv("INV-UNI-1")).json()
    assert first["result"]["verdict"] == "PASS"

    # Zero-width and fullwidth NFKC duplicate invoice_number variants must still trigger DUPLICATE_INVOICE_NUMBER.
    for variant in ("INV\u200b-UNI-1", "ＩＮＶ－ＵＮＩ－１"):
        dup = post(client, inv(variant)).json()
        assert dup["result"]["verdict"] == "BLOCKED"
        assert any(a["code"] == "DUPLICATE_INVOICE_NUMBER" for a in dup["result"]["anomalies"])

    # Cyrillic homoglyph VAT/IBAN ('Е' U+0415) must fail closed as INVALID_IBAN + UNAPPROVED_VENDOR.
    homoglyph_inv = inv("INV-HOMO-1")
    homoglyph_inv["vendor"]["vat"] = "\u0415SB12345674"
    homoglyph_inv["vendor"]["iban"] = "\u0415S9121000418450200051332"
    homo_res = post(client, homoglyph_inv).json()["result"]
    assert homo_res["verdict"] == "BLOCKED"
    homo_codes = {a["code"] for a in homo_res["anomalies"]}
    assert {"INVALID_IBAN", "UNAPPROVED_VENDOR"} <= homo_codes

    # NFD/NFKC/zero-width FTS5 queries must not raise 500s.
    for q in ("e\u0301clair", "ＰＡＳＳ", "INV\u200b-UNI-1"):
        assert client.get("/v1/ledger", headers=H, params={"q": q}).status_code == 200


def test_api_lifespan_closes_ledger_for_tempdir_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with TestClient(create_app(str(tmp_dir / "lifespan.db"), KEY)) as c:
            assert post(c, inv("LIFE-1")).status_code == 200
            assert c.get("/v1/ledger/verify", headers=H).json()["ok"] is True
    assert not tmp_dir.exists()


def test_csv_export_neutralises_unicode_format_and_nfkc_formulas(client):
    vectors = [
        ("CSV-BOM", "\ufeff=1+1"),
        ("CSV-ZWSP", "\u200b=1+1"),
        ("CSV-FW", "\uff1d1+1"),
        ("CSV-SHY", "\u00ad=1+1"),
    ]
    for inv_no, vendor_name in vectors:
        payload = inv(inv_no, vendor={"name": vendor_name, "vat": "ESB12345674", "iban": "ES9121000418450200051332"})
        assert post(client, payload).status_code == 200

    r = client.get("/v1/export.csv", headers=H)
    assert r.status_code == 200
    rows = {row["invoice_number"]: row for row in csv.DictReader(io.StringIO(r.text))}
    for inv_no, vendor_name in vectors:
        assert rows[inv_no]["vendor_name"] == "'" + vendor_name
