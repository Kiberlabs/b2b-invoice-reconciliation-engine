"""FastAPI backend: HMAC-protected /v1/* endpoints plus the embedded executive dashboard at GET /dashboard.

Flow: request -> ``require_key`` (SHA-256 digests compared with ``hmac.compare_digest``, constant time)
-> parse invoice -> load vendor history from the ledger -> ``reconcile`` -> append hash-chained ledger row -> JSON.
Run with ``uvicorn invoice_engine.api:create_app --factory`` (``INVOICE_ENGINE_API_KEY`` is mandatory).
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from . import __version__
from .dashboard import DASHBOARD_HTML
from .ledger import Ledger
from .parser import InvoiceParseError, normalize_id, parse_date, parse_invoice
from .reconciler import HistoryEntry, grn_from_dict, po_from_dict, reconcile

# Synthetic demo vendors so the dashboard samples work out of the box; override with INVOICE_ENGINE_APPROVED_VENDORS.
DEFAULT_APPROVED_VENDORS = ("ESB12345674", "DE123456789")

_CSV_COLUMNS = ["seq", "ts", "invoice_number", "vendor_vat", "vendor_name", "issue_date", "currency", "total",
                "verdict", "risk_score", "anomaly_codes", "prev_hash", "entry_hash"]
_CSV_TEXT = {"invoice_number", "vendor_vat", "vendor_name"}
_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'"


class ReconcileRequest(BaseModel):
    """``invoice`` is a JSON object or raw JSON/XML/OCR text; ``po`` / ``grn`` are the other two legs of the match."""

    invoice: str | dict[str, Any]
    format: Literal["auto", "json", "xml", "text"] = "auto"
    po: dict[str, Any] | None = None
    grn: dict[str, Any] | None = None


def _csv_safe(value: Any) -> str:
    """Neutralise spreadsheet formula injection in free-text cells."""
    s = str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def create_app(db_path: str | None = None, api_key: str | None = None, approved_vendors: list[str] | None = None) -> FastAPI:
    """Application factory; refuses to start without an API key (no insecure default)."""
    key = api_key or os.environ.get("INVOICE_ENGINE_API_KEY", "")
    if not key:
        raise RuntimeError("INVOICE_ENGINE_API_KEY is required")
    key_digest = hashlib.sha256(key.encode()).digest()
    if approved_vendors is None:
        env = os.environ.get("INVOICE_ENGINE_APPROVED_VENDORS")
        approved_vendors = [v for v in env.split(",") if v.strip()] if env else list(DEFAULT_APPROVED_VENDORS)
    ledger = Ledger(db_path or os.environ.get("INVOICE_ENGINE_DB", "data/ledger.db"))

    app = FastAPI(title="KiberLabs Invoice Reconciliation Engine", version=__version__)
    header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def require_key(provided: str | None = Depends(header)) -> None:
        digest = hashlib.sha256((provided or "").encode()).digest()
        if not hmac.compare_digest(digest, key_digest):
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_key)])

    @v1.post("/reconcile")
    def reconcile_invoice(req: ReconcileRequest) -> dict[str, Any]:
        """Parse -> 3-way match -> score -> append to the audit ledger."""
        try:
            inv = parse_invoice(req.invoice, req.format)
            po = po_from_dict(req.po) if req.po else None
            grn = grn_from_dict(req.grn) if req.grn else None
        except InvoiceParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise HTTPException(status_code=422, detail=f"invalid po/grn payload: {exc!r}") from exc
        inv_dict = inv.to_dict()
        history = [
            HistoryEntry(h["invoice_number"], h["vendor_vat"], parse_date(h["issue_date"]), Decimal(h["total"]))
            for h in ledger.history(inv_dict["vendor_vat"])
        ]
        result = reconcile(inv, po, grn, approved_vendors, history).to_dict()
        entry = ledger.append(inv_dict, result)
        return {"invoice": inv_dict, "result": result, "ledger": {"seq": entry["seq"], "entry_hash": entry["entry_hash"]}}

    @v1.get("/ledger")
    def list_ledger(limit: int = Query(200, ge=1, le=5000), q: str | None = Query(None, max_length=200)) -> list[dict[str, Any]]:
        """Newest-first audit rows; ``q`` is an FTS5 search over invoice, vendor, verdict and anomaly codes."""
        return ledger.entries(limit, q)

    @v1.get("/ledger/verify")
    def verify_ledger() -> dict[str, Any]:
        """Recompute the SHA-256 chain; ``ok=false`` with ``broken_at`` pinpoints tampering."""
        return ledger.verify_chain_integrity()

    @v1.get("/export.csv")
    def export_csv() -> PlainTextResponse:
        """Full audit export (formula-injection safe)."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(_CSV_COLUMNS)
        for e in reversed(ledger.entries(5000)):
            e["anomaly_codes"] = " ".join(a["code"] for a in e["anomalies"])
            w.writerow([_csv_safe(e[c]) if c in _CSV_TEXT else e[c] for c in _CSV_COLUMNS])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="invoice_audit_export.csv"'})

    app.include_router(v1)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        """Static page; data calls from the page still require the API key."""
        return HTMLResponse(DASHBOARD_HTML, headers={"Content-Security-Policy": _CSP, "X-Content-Type-Options": "nosniff"})

    app.state.ledger = ledger
    return app
