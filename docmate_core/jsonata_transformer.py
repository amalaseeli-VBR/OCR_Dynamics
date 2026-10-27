"""
Docmate JSONata Transformation Module
Translates non-standard or varying extracted invoice tags into the canonical DocmateInvoiceV1 JSON format.
"""

import json
import logging
from typing import Dict, Any, Tuple, List, Optional
import jsonata

logger = logging.getLogger(__name__)

# Standard Default JSONata rule that handles common tag variations (supplierName, Supplier, Name, etc.)
DEFAULT_CANONICAL_JSONATA_RULE = """
{
  "header": {
    "docmateInvoiceId": header.docmateInvoiceId ? header.docmateInvoiceId : docmateInvoiceId,
    "invoiceNumber": header.invoiceNumber ? header.invoiceNumber : (invoiceNumber ? invoiceNumber : (invoiceNo ? invoiceNo : (InvoiceNo ? InvoiceNo : (InvoiceNumber ? InvoiceNumber : (InvNo ? InvNo : Inv_Number))))),
    "invoiceDate": header.invoiceDate ? header.invoiceDate : (invoiceDate ? invoiceDate : (InvoiceDate ? InvoiceDate : InvDate)),
    "dueDate": header.dueDate ? header.dueDate : (dueDate ? dueDate : (DueDate ? DueDate : PaymentDueDate)),
    "currency": header.currency ? header.currency : (currency ? currency : (Currency ? Currency : 'GBP')),
    "supplier": {
      "rawName": header.supplier.rawName ? header.supplier.rawName : (supplierName ? supplierName : (Supplier ? Supplier : (vendorName ? vendorName : (Vendor ? Vendor : (supplier_name ? supplier_name : Name))))),
      "vatRegistrationNo": header.supplier.vatRegistrationNo ? header.supplier.vatRegistrationNo : (supplierVatNo ? supplierVatNo : (supplier_vat_no ? supplier_vat_no : (VATNo ? VATNo : VatRegistrationNumber))),
      "supplierCode": header.supplier.supplierCode ? header.supplier.supplierCode : (supplierCode ? supplierCode : (supplier_code ? supplier_code : VendorNo))
    },
    "customer": {
      "rawName": header.customer.rawName ? header.customer.rawName : (customerName ? customerName : (Customer ? Customer : (customer_name ? customer_name : ClientName))),
      "vatRegistrationNo": header.customer.vatRegistrationNo ? header.customer.vatRegistrationNo : (customerVatNo ? customerVatNo : CustomerVAT),
      "customerCode": header.customer.customerCode ? header.customer.customerCode : (customerCode ? customerCode : CustomerNo)
    },
    "totals": {
      "netAmount": ($exists(header.totals.netAmount) and header.totals.netAmount != null) ? header.totals.netAmount : (($exists(totalNet) and totalNet != null) ? totalNet : (($exists(netAmount) and netAmount != null) ? netAmount : (($exists(NetTotal) and NetTotal != null) ? NetTotal : (($exists(SubTotal) and SubTotal != null) ? SubTotal : $number(net_amount))))),
      "vatAmount": ($exists(header.totals.vatAmount) and header.totals.vatAmount != null) ? header.totals.vatAmount : (($exists(totalVat) and totalVat != null) ? totalVat : (($exists(vatAmount) and vatAmount != null) ? vatAmount : (($exists(VatTotal) and VatTotal != null) ? VatTotal : (($exists(TaxAmount) and TaxAmount != null) ? TaxAmount : $number(vat_amount))))),
      "grossAmount": ($exists(header.totals.grossAmount) and header.totals.grossAmount != null) ? header.totals.grossAmount : (($exists(totalGross) and totalGross != null) ? totalGross : (($exists(grossAmount) and grossAmount != null) ? grossAmount : (($exists(GrossTotal) and GrossTotal != null) ? GrossTotal : (($exists(InvoiceTotal) and InvoiceTotal != null) ? InvoiceTotal : (($exists(TotalAmount) and TotalAmount != null) ? TotalAmount : $number(total_amount))))))
    },
    "metadata": header.metadata ? header.metadata : metadata
  },
  "lines": [
    $map(lines ? lines : (lineItems ? lineItems : (items ? items : line_items)), function($item, $idx) {
      {
        "lineNumber": $item.lineNumber ? $number($item.lineNumber) : ($item.lineNo ? $number($item.lineNo) : ($item.line_number ? $number($item.line_number) : $idx + 1)),
        "itemCode": $item.itemCode ? $item.itemCode : ($item.code ? $item.code : ($item.item_code ? $item.item_code : ($item.Code ? $item.Code : $item.ProductCode))),
        "barcode": $item.barcode ? $item.barcode : ($item.Barcode ? $item.Barcode : $item.EAN),
        "description": $item.description ? $item.description : ($item.ItemDescription ? $item.ItemDescription : ($item.Description ? $item.Description : ($item.Name ? $item.Name : ($item.product_name ? $item.product_name : '')))),
        "quantity": $item.quantity != null ? $number($item.quantity) : ($item.qty != null ? $number($item.qty) : ($item.Qty != null ? $number($item.Qty) : null)),
        "unitPrice": $item.unitPrice != null ? $number($item.unitPrice) : ($item.price != null ? $number($item.price) : ($item.UnitPrice != null ? $number($item.UnitPrice) : null)),
        "netAmount": $item.netAmount != null ? $number($item.netAmount) : ($item.lineNet != null ? $number($item.lineNet) : ($item.amount != null ? $number($item.amount) : ($item.NetAmount != null ? $number($item.NetAmount) : ($item.LineTotal != null ? $number($item.LineTotal) : null)))),
        "vatRate": $item.vatRate != null ? $number($item.vatRate) : ($item.vat_rate != null ? $number($item.vat_rate) : ($item.VatRate != null ? $number($item.VatRate) : ($item.TaxRate != null ? $number($item.TaxRate) : null))),
        "vatAmount": $item.vatAmount != null ? $number($item.vatAmount) : ($item.vat_amount != null ? $number($item.vat_amount) : ($item.VatAmount != null ? $number($item.VatAmount) : null))
      }
    })
  ]
}
"""

class JsonataTransformer:
    """Wrapper class for executing JSONata rules and transforming invoice payloads."""
    
    def __init__(self, rule_expression: Optional[str] = None):
        self.rule_expression = rule_expression or DEFAULT_CANONICAL_JSONATA_RULE
        try:
            self.expr = jsonata.Jsonata(self.rule_expression)
        except Exception as e:
            logger.error(f"Failed to parse JSONata expression: {e}")
            raise ValueError(f"Invalid JSONata expression: {e}")

    def transform(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """Transform raw extracted data dict into canonical Docmate JSON dict."""
        if not raw_data or not isinstance(raw_data, dict):
            raise ValueError("Input raw_data must be a non-empty dictionary")
            
        try:
            result = self.expr.evaluate(raw_data)
            if result is None:
                result = {}
            
            # Ensure basic structural guarantees
            if "header" not in result:
                result["header"] = {}
            lines = result.get("lines")
            if lines is None:
                result["lines"] = []
            elif not isinstance(lines, list):
                result["lines"] = list(lines)
                
            return result
        except Exception as e:
            logger.error(f"Error evaluating JSONata transformation: {e}")
            raise RuntimeError(f"Transformation failed: {e}")

    @staticmethod
    def validate_canonical(canonical_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate canonical structure against core requirements."""
        errors = []
        if not isinstance(canonical_data, dict):
            return False, ["Canonical data must be a dictionary"]
            
        header = canonical_data.get("header")
        if not header or not isinstance(header, dict):
            errors.append("Missing or invalid 'header' block")
        else:
            supplier = header.get("supplier")
            if not supplier or not isinstance(supplier, dict) or not supplier.get("rawName"):
                errors.append("Header missing required 'supplier.rawName'")
            if not header.get("invoiceNumber"):
                errors.append("Header missing required 'invoiceNumber'")
                
        lines = canonical_data.get("lines")
        if lines is None or not isinstance(lines, list):
            errors.append("Missing or invalid 'lines' list")
            
        return len(errors) == 0, errors

def transform_to_canonical(raw_data: Dict[str, Any], custom_rule: Optional[str] = None) -> Dict[str, Any]:
    """Utility helper function to transform raw extracted dict into canonical format."""
    transformer = JsonataTransformer(rule_expression=custom_rule)
    return transformer.transform(raw_data)
