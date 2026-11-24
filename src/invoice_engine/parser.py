"""Multi-format B2B invoice parser: JSON, UBL / Facturae-style XML and noisy OCR plain text.

Flow: ``parse_invoice(raw)`` -> sniff format -> ``_from_json`` | ``_from_xml`` | ``_from_text``
-> normalised ``Invoice`` (Decimal money, ISO date, VAT/IBAN validity flags, parser warnings).
All checks are deterministic and offline: no LLM, no network.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_INPUT_CHARS = 500_000


class InvoiceParseError(ValueError):
    """Raised when the input is unusable (unknown format, missing invoice number or total)."""


# --------------------------------------------------------------------------- validators

# ISO 13616 registry subset (country -> total IBAN length); other countries fall back to 15..34.
_IBAN_LEN = {"ES": 24, "DE": 22, "FR": 27, "GB": 22, "IT": 27, "NL": 18, "PT": 25, "BE": 16, "IE": 22, "AT": 20, "CH": 21}

# Format-only VAT/IVA patterns (no VIES lookup). shortcut: no per-country checksum, add one per market on demand.
_VAT_RE = {
    "ES": r"ES[A-Z0-9]\d{7}[A-Z0-9]",
    "DE": r"DE\d{9}",
    "FR": r"FR[A-Z0-9]{2}\d{9}",
    "IT": r"IT\d{11}",
    "NL": r"NL\d{9}B\d{2}",
    "PT": r"PT\d{9}",
    "BE": r"BE0?\d{9}",
    "IE": r"IE\d[A-Z0-9]\d{5}[A-Z]{1,2}",
    "AT": r"ATU\d{8}",
    "GB": r"GB(?:\d{9}|\d{12}|GD\d{3}|HA\d{3})",
}


def normalize_id(value: str) -> str:
    """Upper-case and strip every non-alphanumeric character (spaces, dots, dashes) from VAT/IBAN ids."""
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def validate_iban(iban: str) -> bool:
    """ISO 13616 check: length by country, then ``int(rearranged) % 97 == 1`` (letters A=10..Z=35)."""
    s = normalize_id(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    expected = _IBAN_LEN.get(s[:2])
    if expected is not None and len(s) != expected:
        return False
    digits = "".join(str(int(ch, 36)) for ch in s[4:] + s[:4])
    return int(digits) % 97 == 1


def validate_vat(vat: str) -> bool:
    """Format-level EU VAT/IVA validation using the per-country pattern table."""
    s = normalize_id(vat)
    pattern = _VAT_RE.get(s[:2])
    return bool(pattern and re.fullmatch(pattern, s))


# --------------------------------------------------------------------------- amounts & dates

_CUR_TOKEN = re.compile(r"€|\bEUR\b|\$|\bUSD\b|£|\bGBP\b", re.I)
_CUR_CODE = {"€": "EUR", "EUR": "EUR", "$": "USD", "USD": "USD", "£": "GBP", "GBP": "GBP"}
_OCR_DIGITS = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})


def detect_currency(text: str, default: str = "EUR") -> str:
    """Return the ISO code of the first currency symbol/code found in ``text``."""
    m = _CUR_TOKEN.search(text or "")
    return _CUR_CODE[m.group(0).upper()] if m else default


def parse_amount(raw: Any) -> Decimal:
    """Parse EU (``1.250,50 EUR``) and US (``$2,400.00``) money into a Decimal.

    The last separator wins as decimal point when both appear; a lone separator followed by exactly
    three digits is a thousands mark (``1.250`` -> 1250) unless the integer part is ``0``.
    shortcut: ``2.500`` in a US document reads as 2500, pass JSON numbers when that matters.
    """
    if isinstance(raw, bool):
        raise InvoiceParseError(f"unparseable amount: {raw!r}")
    if isinstance(raw, (int, float, Decimal)):
        d = raw if isinstance(raw, Decimal) else Decimal(str(raw))
        if not d.is_finite():
            raise InvoiceParseError(f"non-finite amount: {raw!r}")
        return d
    s = _CUR_TOKEN.sub("", str(raw)).strip()
    s = re.sub(r"[\s\u00a0']", "", s)
    if re.fullmatch(r"-?[\dOolI.,]+", s) and re.search(r"\d", s):
        s = s.translate(_OCR_DIGITS)  # OCR confuses O/0 and l/1 inside numeric tokens only
    if not re.fullmatch(r"-?\d[\d.,]*", s):
        raise InvoiceParseError(f"unparseable amount: {raw!r}")
    dot, comma = s.rfind("."), s.rfind(",")
    if dot >= 0 and comma >= 0:
        dec = "." if dot > comma else ","
        tsep = "," if dec == "." else "."
        if s.count(dec) != 1:
            raise InvoiceParseError(f"unparseable amount: {raw!r}")
        int_part, frac_part = s.split(dec)
        if not re.fullmatch(r"\d+", frac_part) or not re.fullmatch(
            r"-?[1-9]\d{0,2}(?:" + re.escape(tsep) + r"\d{3})+", int_part
        ):
            raise InvoiceParseError(f"unparseable amount: {raw!r}")
        s = int_part.replace(tsep, "") + "." + frac_part
    elif dot >= 0 or comma >= 0:
        sep = "." if dot >= 0 else ","
        parts = s.split(sep)
        if len(parts) > 2:
            if not re.fullmatch(r"-?[1-9]\d{0,2}(?:" + re.escape(sep) + r"\d{3})+", s):
                raise InvoiceParseError(f"unparseable amount: {raw!r}")
            s = s.replace(sep, "")
        else:
            head, tail = parts[0].lstrip("-"), parts[1]
            if not tail:
                raise InvoiceParseError(f"unparseable amount: {raw!r}")
            if len(tail) == 3 and head != "0":
                if not (1 <= len(head) <= 3 and not head.startswith("0")):
                    raise InvoiceParseError(f"unparseable amount: {raw!r}")
                s = s.replace(sep, "")
            else:
                s = s.replace(sep, ".")
    try:
        d = Decimal(s)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regexes above
        raise InvoiceParseError(f"unparseable amount: {raw!r}") from exc
    if not d.is_finite():
        raise InvoiceParseError(f"non-finite amount: {raw!r}")
    return d


def parse_date(raw: Any) -> date | None:
    """ISO ``YYYY-MM-DD`` or ``DD/MM/YYYY`` | ``DD.MM.YYYY`` | ``MM/DD/YYYY`` (US only when day > 12)."""
    s = str(raw or "").strip()
    if m := re.match(r"(\d{4})-(\d{2})-(\d{2})", s):
        y, mo, d = map(int, m.groups())
    elif m := re.match(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", s):
        a, b, y = map(int, m.groups())
        mo, d = (a, b) if b > 12 and a <= 12 else (b, a)
    else:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# --------------------------------------------------------------------------- model


@dataclass
class LineItem:
    """One invoice line: ``sku`` is the PO match key (description is the fallback)."""

    sku: str
    description: str
    quantity: Decimal
    unit_price: Decimal

    def __post_init__(self) -> None:
        if not (self.quantity.is_finite() and self.unit_price.is_finite()):
            raise InvoiceParseError("line item quantity and unit_price must be finite")
        if self.quantity < 0 or self.unit_price < 0:
            raise InvoiceParseError("line item quantity and unit_price must be non-negative")

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_price


@dataclass
class Invoice:
    """Normalised invoice shared by every parser; ``warnings`` records parser assumptions."""

    invoice_number: str
    vendor_name: str = ""
    vendor_vat: str = ""
    vendor_iban: str = ""
    issue_date: date | None = None
    currency: str = "EUR"
    po_number: str = ""
    lines: list[LineItem] = field(default_factory=list)
    subtotal: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    source_format: str = "json"
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not (self.subtotal.is_finite() and self.tax.is_finite() and self.total.is_finite()):
            raise InvoiceParseError("invoice subtotal, tax and total must be finite")

    @property
    def vat_valid(self) -> bool:
        return validate_vat(self.vendor_vat)

    @property
    def iban_valid(self) -> bool:
        return validate_iban(self.vendor_iban)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view (Decimals as strings, ISO date) used by the API and the ledger."""
        return {
            "invoice_number": self.invoice_number,
            "vendor_name": self.vendor_name,
            "vendor_vat": normalize_id(self.vendor_vat),
            "vendor_iban": normalize_id(self.vendor_iban),
            "issue_date": self.issue_date.isoformat() if self.issue_date else None,
            "currency": self.currency,
            "po_number": self.po_number,
            "lines": [
                {"sku": ln.sku, "description": ln.description, "quantity": str(ln.quantity), "unit_price": str(ln.unit_price)}
                for ln in self.lines
            ],
            "subtotal": str(self.subtotal),
            "tax": str(self.tax),
            "total": str(self.total),
            "source_format": self.source_format,
            "vat_valid": self.vat_valid,
            "iban_valid": self.iban_valid,
            "warnings": list(self.warnings),
        }


def _finish(inv: Invoice) -> Invoice:
    """Fill derived subtotal, then enforce the two mandatory fields."""
    if not inv.invoice_number:
        raise InvoiceParseError("missing invoice number")
    if inv.total < 0 or inv.subtotal < 0 or inv.tax < 0:
        raise InvoiceParseError("invoice amounts must be non-negative")
    if inv.total == 0 and inv.subtotal == 0:
        raise InvoiceParseError("missing total")
    if inv.subtotal == 0 and inv.lines:
        inv.subtotal = sum((ln.line_total for ln in inv.lines), Decimal("0"))
        inv.warnings.append("subtotal_derived_from_lines")
    return inv


# --------------------------------------------------------------------------- JSON


def _pick(d: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def _money(v: Any) -> Decimal:
    d = parse_amount(v) if v not in (None, "") else Decimal("0")
    if d < 0:
        raise InvoiceParseError(f"negative amount not allowed: {v!r}")
    return d


def _from_json(data: dict[str, Any]) -> Invoice:
    vendor = data.get("vendor") if isinstance(data.get("vendor"), dict) else {}
    raw_lines = data.get("lines", data.get("items", []))
    if not isinstance(raw_lines, list):
        raise InvoiceParseError("invoice lines must be a list")
    lines = []
    for ln in raw_lines:
        if not isinstance(ln, dict):
            continue
        if ln.get("line_total") not in (None, ""):
            _money(ln["line_total"])
        lines.append(
            LineItem(
                sku=str(_pick(ln, "sku", "code", "item_id")),
                description=str(_pick(ln, "description", "name")),
                quantity=_money(_pick(ln, "quantity", "qty", default="0")),
                unit_price=_money(_pick(ln, "unit_price", "price", default="0")),
            )
        )
    return _finish(
        Invoice(
            invoice_number=str(_pick(data, "invoice_number", "number", "invoice_no", "id")).strip(),
            vendor_name=str(_pick(vendor, "name", default=_pick(data, "vendor_name", "supplier"))),
            vendor_vat=str(_pick(vendor, "vat", default=_pick(data, "vendor_vat", "vat_id"))),
            vendor_iban=str(_pick(vendor, "iban", default=_pick(data, "vendor_iban", "iban"))),
            issue_date=parse_date(_pick(data, "issue_date", "date")),
            currency=str(_pick(data, "currency", default="EUR")).upper(),
            po_number=str(_pick(data, "po_number", "po")),
            lines=lines,
            subtotal=_money(_pick(data, "subtotal", "net_amount", default=None)),
            tax=_money(_pick(data, "tax", "tax_amount", "vat_amount", default=None)),
            total=_money(_pick(data, "total", "total_amount", default=None)),
            source_format="json",
        )
    )


# --------------------------------------------------------------------------- XML (UBL / Facturae)


def _ln(e: ET.Element) -> str:
    return e.tag.rsplit("}", 1)[-1]


def _el(root: ET.Element | None, *names: str) -> ET.Element | None:
    """First descendant (document order) whose namespace-stripped tag equals a name; earlier names win."""
    if root is None:
        return None
    for n in names:
        for e in root.iter():
            if _ln(e) == n:
                return e
    return None


def _tx(root: ET.Element | None, *names: str) -> str:
    e = _el(root, *names)
    return (e.text or "").strip() if e is not None else ""


def _from_xml(text: str) -> Invoice:
    # Trust boundary: expat expands internal entities (billion-laughs), so refuse any DTD outright.
    if re.search(r"<!(?:DOCTYPE|ENTITY)", text, re.I):
        raise InvoiceParseError("XML DTD/entity declarations are not allowed")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise InvoiceParseError(f"invalid XML: {exc}") from exc
    if _ln(root) == "Facturae":
        return _from_facturae(root)
    return _from_ubl(root)


def _from_ubl(root: ET.Element) -> Invoice:
    supplier = _el(root, "AccountingSupplierParty")
    money = _el(root, "LegalMonetaryTotal")
    lines = []
    for e in root.iter():
        if _ln(e) != "InvoiceLine":
            continue
        if lt := _tx(e, "LineExtensionAmount", "TotalCost", "GrossAmount"):
            _money(lt)
        item = _el(e, "Item")
        lines.append(
            LineItem(
                sku=_tx(_el(item, "SellersItemIdentification"), "ID"),
                description=_tx(item, "Name", "Description"),
                quantity=_money(_tx(e, "InvoicedQuantity")),
                unit_price=_money(_tx(_el(e, "Price"), "PriceAmount")),
            )
        )
    number = next((c.text or "" for c in root if _ln(c) == "ID"), "").strip()
    return _finish(
        Invoice(
            invoice_number=number,
            vendor_name=_tx(_el(supplier, "PartyName"), "Name") or _tx(supplier, "RegistrationName"),
            vendor_vat=_tx(_el(supplier, "PartyTaxScheme"), "CompanyID"),
            vendor_iban=_tx(_el(root, "PayeeFinancialAccount"), "ID"),
            issue_date=parse_date(_tx(root, "IssueDate")),
            currency=_tx(root, "DocumentCurrencyCode") or "EUR",
            po_number=_tx(_el(root, "OrderReference"), "ID"),
            lines=lines,
            subtotal=_money(_tx(money, "LineExtensionAmount", "TaxExclusiveAmount")),
            tax=_money(_tx(_el(root, "TaxTotal"), "TaxAmount")),
            total=_money(_tx(money, "PayableAmount", "TaxInclusiveAmount")),
            source_format="xml",
        )
    )


def _from_facturae(root: ET.Element) -> Invoice:
    seller = _el(root, "SellerParty")
    vat = _tx(seller, "TaxIdentificationNumber")
    if vat and not vat[:2].isalpha():
        vat = "ES" + vat  # Facturae omits the country prefix for Spanish taxpayers
    lines = []
    for e in root.iter():
        if _ln(e) != "InvoiceLine":
            continue
        if lt := _tx(e, "LineExtensionAmount", "TotalCost", "GrossAmount"):
            _money(lt)
        lines.append(
            LineItem(
                sku=_tx(e, "ArticleCode"),
                description=_tx(e, "ItemDescription"),
                quantity=_money(_tx(e, "Quantity")),
                unit_price=_money(_tx(e, "UnitPriceWithoutTax")),
            )
        )
    return _finish(
        Invoice(
            invoice_number=_tx(root, "InvoiceNumber"),
            vendor_name=_tx(seller, "CorporateName"),
            vendor_vat=vat,
            vendor_iban=_tx(_el(root, "AccountToBeCredited"), "IBAN"),
            issue_date=parse_date(_tx(root, "IssueDate")),
            currency=_tx(root, "InvoiceCurrencyCode") or "EUR",
            po_number=_tx(root, "ReceiverTransactionReference", "ReceiverContractReference"),
            lines=lines,
            subtotal=_money(_tx(root, "TotalGrossAmountBeforeTaxes")),
            tax=_money(_tx(root, "TotalTaxOutputs")),
            total=_money(_tx(root, "InvoiceTotal")),
            source_format="xml",
        )
    )


# --------------------------------------------------------------------------- OCR plain text

_AMT = r"(?P<v>-?[\dOolI][\dOolI.,\s\u00a0']*)\s*(?:EUR|USD|GBP|€|\$|£)?\s*$"
_CUR = r"(?:EUR|USD|GBP|€|\$|£)?\s*"
_TEXT_FIELDS: dict[str, re.Pattern[str]] = {
    "invoice_number": re.compile(
        r"^\s*(?:[il1]nvo[il1]ce\s*(?:no|nr|number|num|#)|factura\s*(?:n[º°o]|num(?:ero)?|#)|inv\s*#)\.?\s*[:#\-]?\s*(?P<v>\S+)", re.I
    ),
    "po_number": re.compile(
        r"^\s*(?:p\.?o\.?|purchase\s*order|orden\s*de\s*compra|pedido)\b\s*(?:no\.?|number|#|n[º°])?\s*[:#\-]?\s*(?P<v>\S+)", re.I
    ),
    "issue_date": re.compile(r"^\s*(?:invoice\s*date|issue\s*date|date|fecha(?:\s*(?:de\s*)?(?:emisi[oó]n|factura))?)\s*[:#\-]?\s*(?P<v>.+?)\s*$", re.I),
    "vendor_name": re.compile(r"^\s*(?:vendor|supplier|seller|from|proveedor|emisor)\s*[:#\-]\s*(?P<v>.+?)\s*$", re.I),
    "vendor_iban": re.compile(r"^\s*(?:iban|bank\s*account)\s*[:#\-]?\s*(?P<v>[A-Za-z0-9 \-]{12,45}?)\s*$", re.I),
}
_VAT_ID = re.compile(r"^\s*(?:vat(?:\s*(?:id|no\.?|number|reg\.?))?|iva\s*(?:id|no)|tax\s*id|nif|cif)\s*[:#]\s*(?P<v>[A-Za-z0-9 .\-]{8,20}?)\s*$", re.I)
_SUBTOTAL = re.compile(r"^\s*(?:sub\s*-?\s*tota[l1]|net(?:\s*amount)?|base\s*imponible|importe\s*neto)\b[^\d\n-]*" + _AMT, re.I)
_TAX = re.compile(
    r"^\s*(?:tota[l1]\s+)?(?:vat|iva|tax|impuestos?)\b(?![^:\n]*(?:\b(?:id|no|nr|num|number|reg|registration|code|ref)\b|\bn[º°]))[^:\n]*?(?::|\s)\s*"
    + _CUR
    + _AMT,
    re.I,
)
_TOTAL = re.compile(r"^\s*(?:grand\s*)?tota[l1]\b(?:\s*(?:due|amount|a\s*pagar|factura))?[^\d\n-]*" + _AMT, re.I)
_LINE = re.compile(
    r"^\s*(?P<sku>[A-Z][A-Z0-9]*-[A-Z0-9\-]+)\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s*[x×*]\s*(?P<price>\S+)", re.I
)


def _from_text(text: str) -> Invoice:
    inv = Invoice(invoice_number="", source_format="text")
    seen: set[str] = set()
    for raw in text.replace("\u00a0", " ").splitlines():
        line = raw.strip()
        if not line:
            continue
        if m := _LINE.match(line):
            inv.lines.append(LineItem(m["sku"].upper(), m["desc"].strip(), parse_amount(m["qty"]), parse_amount(m["price"])))
            continue
        if (m := _VAT_ID.match(line)) and "vendor_vat" not in seen:
            v = normalize_id(m["v"])
            if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{7,12}", v) or re.match(r"^\s*(?:nif|cif|tax)", line, re.I):
                inv.vendor_vat, _ = v, seen.add("vendor_vat")
                continue
        for key, rx in (("subtotal", _SUBTOTAL), ("tax", _TAX), ("total", _TOTAL)):
            if (m := rx.match(line)) and key not in seen:
                try:
                    setattr(inv, key, parse_amount(m["v"]))
                except InvoiceParseError:
                    inv.warnings.append(f"unreadable_{key}")
                seen.add(key)
                break
        else:
            for key, rx in _TEXT_FIELDS.items():
                if (m := rx.match(line)) and key not in seen:
                    val = m["v"].strip()
                    if key == "issue_date":
                        inv.issue_date = parse_date(val)
                    else:
                        setattr(inv, key, val.upper() if key == "vendor_iban" else val)
                    seen.add(key)
                    break
    inv.currency = detect_currency(text, default="EUR")
    if _CUR_TOKEN.search(text) is None:
        inv.warnings.append("currency_assumed_eur")
    return _finish(inv)


# --------------------------------------------------------------------------- entry point


def parse_invoice(raw: str | dict[str, Any], fmt: str = "auto") -> Invoice:
    """Parse one invoice. ``fmt``: ``auto`` (sniff), ``json``, ``xml`` or ``text``."""
    if isinstance(raw, dict):
        return _from_json(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise InvoiceParseError("empty invoice payload")
    if len(raw) > MAX_INPUT_CHARS:
        raise InvoiceParseError("invoice payload too large")
    head = raw.lstrip()
    if fmt == "auto":
        fmt = "json" if head.startswith("{") else "xml" if head.startswith("<") else "text"
    if fmt == "json":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvoiceParseError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise InvoiceParseError("JSON invoice must be an object")
        return _from_json(data)
    if fmt == "xml":
        return _from_xml(head)
    if fmt == "text":
        return _from_text(raw)
    raise InvoiceParseError(f"unknown format: {fmt}")
