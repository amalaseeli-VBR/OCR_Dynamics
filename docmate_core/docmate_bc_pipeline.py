"""
Docmate to Business Central (BC) Integrated Pipeline Orchestrator
Coordinates: Raw OCR/Extracted Input -> JSONata Normalization -> Customer BC Enrichment -> Canonical Payload Generation
"""

import logging
import json
from typing import Dict, Any, Optional, Tuple, List
from docmate_core.jsonata_transformer import JsonataTransformer, transform_to_canonical
from docmate_core.supplier_registry import SupplierRegistry
from docmate_core.customer_enrichment import CustomerEnrichmentEngine

logger = logging.getLogger(__name__)

class DocmateBcPipeline:
    """Full processing pipeline for translating extracted raw invoice JSON into enriched canonical BC payloads."""

    def __init__(self, 
                 supplier_registry: Optional[SupplierRegistry] = None, 
                 customer_config: Optional[Dict[str, Any]] = None):
        self.supplier_registry = supplier_registry or SupplierRegistry()
        self.enrichment_engine = CustomerEnrichmentEngine(mapping_config=customer_config)

    def process_invoice(self, raw_invoice_data: Dict[str, Any], override_supplier_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute full pipeline:
        1. Determine supplier name from raw payload or override.
        2. Retrieve supplier-specific JSONata rule or fallback to standard canonical rule.
        3. Transform raw payload to Canonical DocmateInvoiceV1 JSON format.
        4. Validate canonical structure.
        5. Enrich with customer-specific BC master data (Vendor No, GL Account, Dimensions).
        """
        if not raw_invoice_data or not isinstance(raw_invoice_data, dict):
            raise ValueError("raw_invoice_data must be a non-empty dictionary")

        # Step 1: Detect supplier name
        supplier_name = override_supplier_name or raw_invoice_data.get("supplierName") or raw_invoice_data.get("Supplier") or raw_invoice_data.get("vendorName") or ""
        
        # Step 2: Get JSONata rule
        custom_rule = self.supplier_registry.get_rule(supplier_name)
        
        # Step 3: Transform payload
        transformer = JsonataTransformer(rule_expression=custom_rule)
        canonical_payload = transformer.transform(raw_invoice_data)
        
        # Step 4: Validate canonical schema
        is_valid, errors = JsonataTransformer.validate_canonical(canonical_payload)
        if not is_valid:
            logger.warning(f"Canonical validation warnings for supplier '{supplier_name}': {errors}")
            
        # Step 5: Enrich with customer BC settings
        enriched_payload = self.enrichment_engine.enrich(canonical_payload)
        
        return enriched_payload

    def prepare_bc_buffer_records(self, enriched_payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Extract flat header dict and list of line dicts ready for insertion into BC buffer tables
        (e.g., dbo.DynamicsInvoiceHeader and dbo.DynamicsInvoiceLine).
        """
        header = enriched_payload.get("header", {})
        supplier = header.get("supplier", {})
        totals = header.get("totals", {})
        bc_integration = header.get("bcIntegration", {})

        buffer_header = {
            "InvoiceNumber": header.get("invoiceNumber", ""),
            "InvoiceDate": header.get("invoiceDate", ""),
            "DueDate": header.get("dueDate", ""),
            "CurrencyCode": header.get("currency", "GBP"),
            "SupplierName": supplier.get("rawName", ""),
            "SupplierVatNo": supplier.get("vatRegistrationNo", ""),
            "BcVendorNo": bc_integration.get("bcVendorNo", ""),
            "BcGlAccount": bc_integration.get("bcGlAccount", ""),
            "TotalNet": totals.get("netAmount"),
            "TotalVat": totals.get("vatAmount"),
            "TotalGross": totals.get("grossAmount"),
            "DimensionsJson": json.dumps(bc_integration.get("dimensions", {}))
        }

        buffer_lines = []
        for line in enriched_payload.get("lines", []):
            line_bc = line.get("bcIntegration", {})
            buffer_lines.append({
                "LineNumber": line.get("lineNumber"),
                "ItemCode": line.get("itemCode", ""),
                "Barcode": line.get("barcode", ""),
                "Description": line.get("description", ""),
                "Quantity": line.get("quantity"),
                "UnitPrice": line.get("unitPrice"),
                "NetAmount": line.get("netAmount"),
                "VatRate": line.get("vatRate"),
                "VatAmount": line.get("vatAmount"),
                "BcGlAccount": line_bc.get("bcGlAccount", ""),
                "DimensionsJson": json.dumps(line_bc.get("dimensions", {}))
            })

        return buffer_header, buffer_lines
