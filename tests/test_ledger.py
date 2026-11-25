from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from typing import Any

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


def test_iter_entries_and_empty_vat_history(ledger):
    fill(ledger, n=5)
    streamed = list(ledger.iter_entries(batch_size=2))
    assert [r["seq"] for r in streamed] == [1, 2, 3, 4, 5]
    assert ledger.history("") == []


def test_multithread_close_releases_all_handles_for_tempdir_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "mt_close.db"
        ledger = Ledger(db_path)
        barrier = threading.Barrier(8)

        def worker(tid: int) -> None:
            barrier.wait(timeout=5.0)
            ledger.append({**INV, "invoice_number": f"MT-{tid}"}, RES)
            _ = ledger.history(INV["vendor_vat"])
            _ = ledger.entries(limit=10)
            _ = ledger.verify_chain_integrity()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(worker, tid) for tid in range(8)]
            for fut in futs:
                fut.result()

        rep = ledger.verify_chain_integrity()
        assert rep["ok"] and rep["entries"] == 8

        ledger.close()
        ledger.close()  # idempotent

        with pytest.raises(RuntimeError, match="closed"):
            ledger.append(INV, RES)
        with pytest.raises(RuntimeError, match="closed"):
            ledger.history(INV["vendor_vat"])
        with pytest.raises(RuntimeError, match="closed"):
            ledger.entries()
        with pytest.raises(RuntimeError, match="closed"):
            ledger.verify_chain_integrity()


def test_reentrant_transaction_and_rollback(tmp_path):
    with Ledger(tmp_path / "tx.db") as ledger:
        with ledger.transaction():
            _ = ledger.history(INV["vendor_vat"])
            ledger.append({**INV, "invoice_number": "TX-1"}, RES)
            with ledger.transaction():
                ledger.append({**INV, "invoice_number": "TX-2"}, RES)
        assert ledger.verify_chain_integrity()["entries"] == 2

        with pytest.raises(ValueError, match="boom"):
            with ledger.transaction():
                ledger.append({**INV, "invoice_number": "TX-ROLLBACK"}, RES)
                raise ValueError("boom")
        rep = ledger.verify_chain_integrity()
        assert rep["ok"] and rep["entries"] == 2

        with ledger.transaction():
            ledger.append({**INV, "invoice_number": "TX-OUTER"}, RES)
            with pytest.raises(ValueError, match="inner"):
                with ledger.transaction():
                    ledger.append({**INV, "invoice_number": "TX-INNER-RB"}, RES)
                    raise ValueError("inner")
        rep2 = ledger.verify_chain_integrity()
        assert rep2["ok"] and rep2["entries"] == 3
        assert [e["invoice_number"] for e in ledger.iter_entries()] == ["TX-1", "TX-2", "TX-OUTER"]


class _LedgerSpyConn:
    def __init__(self, inner: sqlite3.Connection, sql_log: list[str], fail_hook: Any = None) -> None:
        self._inner = inner
        self._sql_log = sql_log
        self._fail_hook = fail_hook
        self.closed = False

    @property
    def row_factory(self) -> Any:
        return self._inner.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._inner.row_factory = value

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        normalized = " ".join(sql.split()).rstrip(";").upper()
        self._sql_log.append(normalized)
        if self._fail_hook is not None:
            self._fail_hook("execute", normalized)
        return self._inner.execute(sql, *args, **kwargs)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        self._sql_log.append("EXECUTESCRIPT")
        if self._fail_hook is not None:
            self._fail_hook("executescript", sql_script)
        return self._inner.executescript(sql_script)

    def commit(self) -> None:
        self._inner.commit()

    def close(self) -> None:
        self.closed = True
        self._inner.close()

    def __enter__(self) -> _LedgerSpyConn:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: object) -> bool | None:
        return self._inner.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_ledger_pragma_order_and_skips_redundant_wal(monkeypatch: pytest.MonkeyPatch) -> None:
    sql_log: list[str] = []
    timeouts: list[object] = []
    real_connect = sqlite3.connect

    def spy_connect(*args: object, **kwargs: object) -> _LedgerSpyConn:
        timeouts.append(kwargs.get("timeout"))
        return _LedgerSpyConn(real_connect(*args, **kwargs), sql_log)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "ledger_pragma.db"
        with Ledger(db_path):
            assert timeouts == [5.0]
            busy_idx = sql_log.index("PRAGMA BUSY_TIMEOUT=5000")
            mode_idx = sql_log.index("PRAGMA JOURNAL_MODE")
            wal_idx = sql_log.index("PRAGMA JOURNAL_MODE=WAL")
            assert busy_idx < mode_idx < wal_idx

        sql_log.clear()
        with Ledger(db_path) as ledger:
            assert "PRAGMA BUSY_TIMEOUT=5000" in sql_log
            assert "PRAGMA JOURNAL_MODE" in sql_log
            assert "PRAGMA JOURNAL_MODE=WAL" not in sql_log

            sql_log.clear()
            err_holder: list[BaseException] = []

            def worker() -> None:
                try:
                    with ledger._use() as db:
                        assert db.execute("SELECT 1").fetchone()[0] == 1
                except BaseException as exc:  # pragma: no cover
                    err_holder.append(exc)

            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=5.0)
            assert err_holder == []
            assert "PRAGMA BUSY_TIMEOUT=5000" in sql_log
            assert "PRAGMA JOURNAL_MODE=WAL" not in sql_log

    sql_log.clear()
    with Ledger(":memory:") as mem_ledger:
        mem_ledger.append(INV, RES)
        assert mem_ledger.verify_chain_integrity()["entries"] == 1
        assert "PRAGMA JOURNAL_MODE=WAL" not in sql_log


def test_ledger_cold_start_retry_and_tempdir_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = sqlite3.connect
    monkeypatch.setattr(time, "sleep", lambda _secs: None)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        db_path = tmp_dir / "retry_wal.db"
        conns: list[_LedgerSpyConn] = []
        sql_log: list[str] = []

        def flaky_wal_connect(*args: object, **kwargs: object) -> _LedgerSpyConn:
            attempt = len(conns) + 1

            def fail_hook(kind: str, sql: str) -> None:
                if kind == "execute" and sql == "PRAGMA JOURNAL_MODE=WAL" and attempt <= 2:
                    raise sqlite3.OperationalError("database is locked")

            spy = _LedgerSpyConn(real_connect(*args, **kwargs), sql_log, fail_hook)  # type: ignore[arg-type]
            conns.append(spy)
            return spy

        monkeypatch.setattr(sqlite3, "connect", flaky_wal_connect)
        ledger = Ledger(db_path)
        assert len(conns) == 3
        assert conns[0].closed and conns[1].closed
        assert not conns[2].closed
        row = ledger.append(INV, RES)
        assert row["seq"] == 1
        ledger.close()
        assert conns[2].closed

    assert not tmp_dir.exists()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        db_path = tmp_dir / "retry_schema.db"
        conns = []
        sql_log = []

        def flaky_schema_connect(*args: object, **kwargs: object) -> _LedgerSpyConn:
            attempt = len(conns) + 1

            def fail_hook(kind: str, _sql: str) -> None:
                if kind == "executescript" and attempt == 1:
                    raise sqlite3.OperationalError("database is locked")

            spy = _LedgerSpyConn(real_connect(*args, **kwargs), sql_log, fail_hook)  # type: ignore[arg-type]
            conns.append(spy)
            return spy

        monkeypatch.setattr(sqlite3, "connect", flaky_schema_connect)
        ledger = Ledger(db_path)
        assert len(conns) == 2
        assert conns[0].closed
        assert not conns[1].closed
        assert ledger.append(INV, RES)["seq"] == 1
        ledger.close()
        assert conns[1].closed

    assert not tmp_dir.exists()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        db_path = tmp_dir / "always_locked.db"
        conns = []
        sql_log = []

        def always_locked_connect(*args: object, **kwargs: object) -> _LedgerSpyConn:
            def fail_hook(kind: str, sql: str) -> None:
                if kind == "execute" and sql == "PRAGMA JOURNAL_MODE=WAL":
                    raise sqlite3.OperationalError("database is locked")

            spy = _LedgerSpyConn(real_connect(*args, **kwargs), sql_log, fail_hook)  # type: ignore[arg-type]
            conns.append(spy)
            return spy

        monkeypatch.setattr(sqlite3, "connect", always_locked_connect)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            Ledger(db_path)
        assert len(conns) == 6
        assert all(c.closed for c in conns)

    assert not tmp_dir.exists()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        db_path = tmp_dir / "non_lock.db"
        conns = []
        sql_log = []

        def non_lock_connect(*args: object, **kwargs: object) -> _LedgerSpyConn:
            def fail_hook(kind: str, sql: str) -> None:
                if kind == "execute" and sql == "PRAGMA JOURNAL_MODE=WAL":
                    raise sqlite3.OperationalError("disk I/O error")

            spy = _LedgerSpyConn(real_connect(*args, **kwargs), sql_log, fail_hook)  # type: ignore[arg-type]
            conns.append(spy)
            return spy

        monkeypatch.setattr(sqlite3, "connect", non_lock_connect)
        with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
            Ledger(db_path)
        assert len(conns) == 1
        assert conns[0].closed

    assert not tmp_dir.exists()
