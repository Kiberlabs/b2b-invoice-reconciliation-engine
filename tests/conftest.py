from decimal import Decimal

import pytest

from invoice_engine.parser import Invoice, LineItem
from invoice_engine.reconciler import GoodsReceipt, POLine, PurchaseOrder

GOOD_IBAN = "ES9121000418450200051332"
GOOD_VAT = "ESB12345674"


@pytest.fixture
def invoice() -> Invoice:
    """A clean invoice that matches ``po`` and ``grn`` exactly."""
    from datetime import date

    return Invoice(
        invoice_number="INV-1", vendor_name="Northwind S.L.", vendor_vat=GOOD_VAT, vendor_iban=GOOD_IBAN,
        issue_date=date(2026, 11, 20), po_number="PO-1",
        lines=[LineItem("CMP-1", "Board", Decimal("10"), Decimal("25.00"))],
        subtotal=Decimal("250.00"), tax=Decimal("52.50"), total=Decimal("302.50"),
    )


@pytest.fixture
def po() -> PurchaseOrder:
    return PurchaseOrder("PO-1", GOOD_VAT, [POLine("CMP-1", Decimal("10"), Decimal("25.00"))])


@pytest.fixture
def grn() -> GoodsReceipt:
    return GoodsReceipt("PO-1", {"CMP1": Decimal("10")})
