import csv
import io
import sqlite3

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
