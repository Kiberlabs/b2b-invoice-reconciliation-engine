import sqlite3
import threading

import pytest

from invoice_engine.ledger import GENESIS_HASH, Ledger

INV = {"invoice_number": "INV-1", "vendor_vat": "ESB12345674", "vendor_name": "Northwind", "issue_date": "2026-11-20",
       "currency": "EUR", "total": "302.50"}
RES = {"verdict": "REVIEW", "risk_score": 40, "anomalies": [{"code": "ARITHMETIC_ERROR", "message": "m", "points": 40, "critical": False}]}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "l.db")


def fill(ledger, n=3):
    return [ledger.append({**INV, "invoice_number": f"INV-{i}"}, RES) for i in range(1, n + 1)]


def test_wal_and_busy_timeout_enabled(ledger):
    with ledger._use() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_chain_links_and_verifies(ledger):
    rows = fill(ledger)
    assert rows[0]["prev_hash"] == GENESIS_HASH
    assert rows[1]["prev_hash"] == rows[0]["entry_hash"] and rows[2]["prev_hash"] == rows[1]["entry_hash"]
    rep = ledger.verify_chain_integrity()
    assert rep["ok"] and rep["entries"] == 3 and rep["head_hash"] == rows[2]["entry_hash"]


def test_tampered_content_detected(ledger):
    fill(ledger)
    db = sqlite3.connect(ledger.path)
    db.execute("UPDATE audit_entries SET verdict='PASS', risk_score=0 WHERE seq=2")
    db.commit(); db.close()
    rep = ledger.verify_chain_integrity()
    assert not rep["ok"] and rep["broken_at"] == 2 and "modified" in rep["reason"]


def test_deleted_row_detected(ledger):
    fill(ledger)
    db = sqlite3.connect(ledger.path)
    db.execute("DELETE FROM audit_entries WHERE seq=2")
    db.commit(); db.close()
    rep = ledger.verify_chain_integrity()
    assert not rep["ok"] and rep["broken_at"] == 3


def test_tail_truncation_needs_pinned_head(ledger):
    rows = fill(ledger)
    db = sqlite3.connect(ledger.path)
    db.execute("DELETE FROM audit_entries WHERE seq=3")
    db.commit(); db.close()
    assert ledger.verify_chain_integrity()["ok"]  # documented limit
    assert not ledger.verify_chain_integrity(expected_head=rows[2]["entry_hash"])["ok"]


def test_fts5_search_and_injection_safe(ledger):
    fill(ledger)
    assert len(ledger.entries(query="arithmetic_error")) == 3
    assert [e["invoice_number"] for e in ledger.entries(query="INV-2")] == ["INV-2"]
    assert ledger.entries(query='" OR 1=1 --') == []  # quoted tokens, no FTS syntax error


def test_concurrent_writers_keep_chain_valid(ledger):
    errors = []

    def work(k):
        try:
            for i in range(10):
                ledger.append({**INV, "invoice_number": f"T{k}-{i}"}, RES)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors
    rep = ledger.verify_chain_integrity()
    assert rep["ok"] and rep["entries"] == 40
