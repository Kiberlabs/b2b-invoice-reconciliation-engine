"""Tamper-evident audit ledger: SQLite WAL + FTS5 search + SHA-256 hash chain.

Flow: ``append()`` opens ``BEGIN IMMEDIATE`` (serialises writers across processes) -> reads the chain head
-> ``entry_hash = sha256(prev_hash | canonical row JSON)`` -> inserts row + FTS row. ``verify_chain_integrity()``
recomputes every hash from genesis and reports the first broken sequence number.
Editing, deleting or reordering a mid-chain row breaks verification. Truncating the tail is only caught if the
caller pins ``expected_head`` (the value returned by ``head_hash()``) somewhere outside the database.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_entries (
    seq INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    vendor_vat TEXT NOT NULL,
    vendor_name TEXT NOT NULL,
    issue_date TEXT,
    currency TEXT NOT NULL,
    total TEXT NOT NULL,
    verdict TEXT NOT NULL,
    risk_score INTEGER NOT NULL,
    anomalies TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE
);
CREATE VIRTUAL TABLE IF NOT EXISTS audit_fts USING fts5(invoice_number, vendor_name, verdict, anomalies);
"""

_HASHED = ("seq", "ts", "invoice_number", "vendor_vat", "vendor_name", "issue_date", "currency", "total", "verdict", "risk_score", "anomalies")


def _entry_hash(prev_hash: str, row: dict[str, Any]) -> str:
    """Hash of the previous hash plus the canonical (sorted, compact) JSON of every audited column."""
    body = json.dumps({k: row[k] for k in _HASHED}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"{prev_hash}|{body}".encode()).hexdigest()


class Ledger:
    """Append-only, hash-chained audit log. One cached connection per thread (reopening WAL per call costs ~1s on Windows)."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conns: set[sqlite3.Connection] = set()
        self._closed = False
        self._local = threading.local()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._use() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _use(self) -> Iterator[sqlite3.Connection]:
        """Yield this thread's connection, creating it with the WAL pragmas on first use."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Ledger is closed")
            db = getattr(self._local, "db", None)
            if db is None or db not in self._conns:
                db = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)  # autocommit; one conn per thread, closable from any thread
                try:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("PRAGMA busy_timeout=5000")
                    db.execute("PRAGMA synchronous=NORMAL")
                except BaseException:
                    db.close()
                    raise
                self._local.db = db
                self._conns.add(db)
        yield db

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Reentrant write transaction: outer uses BEGIN IMMEDIATE, nested uses SAVEPOINT."""
        with self._lock:
            with self._use() as db:
                depth = getattr(self._local, "tx_depth", 0)
                use_savepoint = depth > 0 or db.in_transaction
                sp = f"sp_{depth}"
                if use_savepoint:
                    db.execute(f"SAVEPOINT {sp}")
                else:
                    db.execute("BEGIN IMMEDIATE")
                self._local.tx_depth = depth + 1
                try:
                    yield
                except BaseException:
                    if use_savepoint:
                        db.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        db.execute(f"RELEASE SAVEPOINT {sp}")
                    elif db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
                else:
                    if use_savepoint:
                        db.execute(f"RELEASE SAVEPOINT {sp}")
                    elif db.in_transaction:
                        db.execute("COMMIT")
                finally:
                    self._local.tx_depth = depth

    def close(self) -> None:
        """Close all tracked per-thread connections and mark the ledger closed (idempotent)."""
        with self._lock:
            self._closed = True
            conns = list(self._conns)
            self._conns.clear()
            self._local.db = None
            self._local.tx_depth = 0
            for db in conns:
                try:
                    db.close()
                except sqlite3.Error:
                    pass

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def append(self, invoice: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """Record one reconciliation outcome; returns the stored row including its ``entry_hash``."""
        with self.transaction():
            with self._use() as db:
                last = db.execute("SELECT seq, entry_hash FROM audit_entries ORDER BY seq DESC LIMIT 1").fetchall()
                seq, prev = (last[0]["seq"] + 1, last[0]["entry_hash"]) if last else (1, GENESIS_HASH)
                row = {
                    "seq": seq,
                    "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    "invoice_number": invoice["invoice_number"],
                    "vendor_vat": invoice["vendor_vat"],
                    "vendor_name": invoice["vendor_name"],
                    "issue_date": invoice["issue_date"],
                    "currency": invoice["currency"],
                    "total": invoice["total"],
                    "verdict": result["verdict"],
                    "risk_score": result["risk_score"],
                    "anomalies": json.dumps(result["anomalies"], sort_keys=True, separators=(",", ":")),
                }
                row["prev_hash"] = prev
                row["entry_hash"] = _entry_hash(prev, row)
                cols = ",".join(row)
                db.execute(f"INSERT INTO audit_entries ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
                codes = " ".join(a["code"] for a in result["anomalies"])
                db.execute(
                    "INSERT INTO audit_fts (rowid, invoice_number, vendor_name, verdict, anomalies) VALUES (?,?,?,?,?)",
                    (seq, row["invoice_number"], row["vendor_name"], row["verdict"], codes),
                )
        return row

    def entries(self, limit: int = 200, query: str | None = None) -> list[dict[str, Any]]:
        """Newest-first rows; ``query`` runs an FTS5 search (tokens are quoted, so user text cannot inject syntax)."""
        limit = max(1, min(int(limit), 5000))
        with self._use() as db:
            if query and (tokens := re.findall(r"\w+", query)):
                match = " ".join(f'"{t}"*' for t in tokens)
                rows = db.execute(
                    "SELECT e.* FROM audit_entries e JOIN audit_fts f ON f.rowid = e.seq "
                    "WHERE audit_fts MATCH ? ORDER BY e.seq DESC LIMIT ?",
                    (match, limit),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM audit_entries ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["anomalies"] = json.loads(d["anomalies"])
            out.append(d)
        return out

    def iter_entries(self, batch_size: int = 1000) -> Iterator[dict[str, Any]]:
        """Stream all entries in ascending ``seq`` order without a row-count cap."""
        step = max(1, int(batch_size))
        last_seq = 0
        while True:
            with self._use() as db:
                rows = db.execute(
                    "SELECT * FROM audit_entries WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                    (last_seq, step),
                ).fetchall()
            if not rows:
                break
            for r in rows:
                d = dict(r)
                d["anomalies"] = json.loads(d["anomalies"])
                last_seq = int(d["seq"])
                yield d

    def history(self, vendor_vat: str) -> list[dict[str, Any]]:
        """Prior invoices for one vendor (used by the duplicate rules)."""
        if not vendor_vat:
            return []
        with self._use() as db:
            rows = db.execute(
                "SELECT invoice_number, vendor_vat, issue_date, total FROM audit_entries WHERE vendor_vat = ?", (vendor_vat,)
            ).fetchall()
        return [dict(r) for r in rows]

    def head_hash(self) -> str:
        with self._use() as db:
            last = db.execute("SELECT entry_hash FROM audit_entries ORDER BY seq DESC LIMIT 1").fetchall()
        return last[0]["entry_hash"] if last else GENESIS_HASH

    def verify_chain_integrity(self, expected_head: str | None = None) -> dict[str, Any]:
        """Recompute the whole chain. Returns ``{ok, entries, broken_at, head_hash, reason}``."""
        prev, count = GENESIS_HASH, 0
        with self._use() as db:
            rows = db.execute("SELECT * FROM audit_entries ORDER BY seq").fetchall()
        for r in rows:
            row = dict(r)
            count += 1
            if row["seq"] != count:
                return self._report(False, count - 1, row["seq"], prev, "sequence gap (row deleted or reordered)")
            if row["prev_hash"] != prev:
                return self._report(False, count - 1, row["seq"], prev, "prev_hash does not match previous entry")
            if _entry_hash(prev, row) != row["entry_hash"]:
                return self._report(False, count - 1, row["seq"], prev, "entry content was modified")
            prev = row["entry_hash"]
        if expected_head is not None and expected_head != prev:
            return self._report(False, count, None, prev, "head hash differs from the pinned value (tail truncated or appended)")
        return self._report(True, count, None, prev, "")

    @staticmethod
    def _report(ok: bool, entries: int, broken_at: int | None, head: str, reason: str) -> dict[str, Any]:
        return {"ok": ok, "entries": entries, "broken_at": broken_at, "head_hash": head, "reason": reason}
