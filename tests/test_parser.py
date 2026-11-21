from datetime import date
from decimal import Decimal

import pytest

from invoice_engine.parser import (
    InvoiceParseError, parse_amount, parse_invoice, validate_iban, validate_vat,
)

UBL = """<?xml version="1.0"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>UBL-77</cbc:ID><cbc:IssueDate>2026-11-18</cbc:IssueDate><cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:OrderReference><cbc:ID>PO-9</cbc:ID></cac:OrderReference>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyName><cbc:Name>Acme Parts GmbH</cbc:Name></cac:PartyName>
    <cac:PartyTaxScheme><cbc:CompanyID>DE123456789</cbc:CompanyID></cac:PartyTaxScheme>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:PaymentMeans><cac:PayeeFinancialAccount><cbc:ID>DE89 3704 0044 0532 0130 00</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>
  <cac:TaxTotal><cbc:TaxAmount>19.00</cbc:TaxAmount></cac:TaxTotal>
  <cac:LegalMonetaryTotal><cbc:LineExtensionAmount>100.00</cbc:LineExtensionAmount><cbc:PayableAmount>119.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
  <cac:InvoiceLine><cbc:ID>1</cbc:ID><cbc:InvoicedQuantity>4</cbc:InvoicedQuantity>
    <cac:Item><cbc:Name>Gasket</cbc:Name><cac:SellersItemIdentification><cbc:ID>GSK-1</cbc:ID></cac:SellersItemIdentification></cac:Item>
    <cac:Price><cbc:PriceAmount>25.00</cbc:PriceAmount></cac:Price></cac:InvoiceLine>
</Invoice>"""

FACTURAE = """<fe:Facturae xmlns:fe="http://www.facturae.gob.es/formato/Versiones/Facturae_3_2_2.xml">
<Parties><SellerParty><TaxIdentification><TaxIdentificationNumber>B12345674</TaxIdentificationNumber></TaxIdentification>
<LegalEntity><CorporateName>Iberia Suministros S.L.</CorporateName></LegalEntity></SellerParty></Parties>
<Invoices><Invoice><InvoiceHeader><InvoiceNumber>FAC-5</InvoiceNumber></InvoiceHeader>
<InvoiceIssueData><IssueDate>2026-11-10</IssueDate><InvoiceCurrencyCode>EUR</InvoiceCurrencyCode></InvoiceIssueData>
<InvoiceTotals><TotalGrossAmountBeforeTaxes>200.00</TotalGrossAmountBeforeTaxes><TotalTaxOutputs>42.00</TotalTaxOutputs><InvoiceTotal>242.00</InvoiceTotal></InvoiceTotals>
<Items><InvoiceLine><ItemDescription>Cable</ItemDescription><Quantity>2</Quantity><UnitPriceWithoutTax>100.00</UnitPriceWithoutTax><ArticleCode>CAB-1</ArticleCode></InvoiceLine></Items>
</Invoice></Invoices></fe:Facturae>"""

OCR = """  lnvoice No : OCR-2026-14
Vendor: Northwind Components S.L.
VAT: ES B1234 5674
IBAN: ES91 2100 0418 4502 0005 1332
Date: 20/11/2026
PO: PO-7001
CMP-100  Controller board  10 x 25,00
Subtotal: 25O,OO EUR
VAT 21%: 52,50 EUR
Total: 302,50 EUR
"""


@pytest.mark.parametrize("raw,expected", [
    ("1.250,50 EUR", "1250.50"), ("$2,400.00", "2400.00"), ("€ 1 250,50", "1250.50"),
    ("2400", "2400"), ("1.250.000,5", "1250000.5"), ("0,500", "0.500"), ("3OO,OO", "300.00"), ("-45,10", "-45.10"),
])
def test_parse_amount_eu_us_ocr(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


def test_parse_amount_rejects_garbage():
    with pytest.raises(InvoiceParseError):
        parse_amount("abc")


@pytest.mark.parametrize("iban,ok", [
    ("ES91 2100 0418 4502 0005 1332", True), ("DE89370400440532013000", True), ("GB82 WEST 1234 5698 7654 32", True),
    ("ES9121000418450200051333", False),  # one digit off
    ("ES91210004184502000513", False),  # wrong length for ES
    ("", False), ("XX", False),
])
def test_iban_mod97(iban, ok):
    assert validate_iban(iban) is ok


def test_vat_format():
    assert validate_vat("ES B1234567-4") and validate_vat("DE123456789")
    assert not validate_vat("DE12345") and not validate_vat("") and not validate_vat("ZZ123456789")


def test_parse_json_nested_vendor_and_eu_amounts():
    inv = parse_invoice('{"invoice_number":"A-1","vendor":{"name":"V","vat":"DE123456789","iban":"DE89370400440532013000"},'
                        '"issue_date":"20/11/2026","total":"1.250,50 EUR","subtotal":"1.033,47","tax":"217,03"}')
    assert inv.total == Decimal("1250.50") and inv.issue_date == date(2026, 11, 20)
    assert inv.vat_valid and inv.iban_valid and inv.source_format == "json"


def test_parse_ubl_xml():
    inv = parse_invoice(UBL)
    assert (inv.invoice_number, inv.po_number, inv.vendor_vat) == ("UBL-77", "PO-9", "DE123456789")
    assert inv.total == Decimal("119.00") and inv.lines[0].sku == "GSK-1" and inv.iban_valid


def test_parse_facturae_xml_adds_es_prefix():
    inv = parse_invoice(FACTURAE)
    assert inv.vendor_vat == "ESB12345674" and inv.vat_valid and inv.total == Decimal("242.00")
    assert inv.lines[0].sku == "CAB-1"


def test_xml_entity_bomb_rejected():
    with pytest.raises(InvoiceParseError, match="DTD"):
        parse_invoice('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Invoice><ID>&a;</ID></Invoice>')


def test_parse_noisy_ocr_text():
    inv = parse_invoice(OCR)
    assert inv.invoice_number == "OCR-2026-14" and inv.vendor_vat == "ESB12345674" and inv.iban_valid
    assert inv.subtotal == Decimal("250.00") and inv.tax == Decimal("52.50") and inv.total == Decimal("302.50")
    assert inv.issue_date == date(2026, 11, 20) and inv.po_number == "PO-7001"
    assert inv.lines[0].quantity == 10 and inv.lines[0].unit_price == Decimal("25.00")


def test_parse_ocr_us_dollars():
    inv = parse_invoice("Invoice #: US-9\nSubtotal $2,400.00\nTax: $192.00\nGrand Total: $2,592.00")
    assert inv.currency == "USD" and inv.total == Decimal("2592.00")


def test_missing_fields_raise():
    for bad in ("", "   ", '{"total": 5}', "Invoice No: X-1", "[1,2]", "{not json"):
        with pytest.raises(InvoiceParseError):
            parse_invoice(bad)
