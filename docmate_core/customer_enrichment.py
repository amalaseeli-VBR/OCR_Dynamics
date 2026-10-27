"""
Docmate Customer & Business Central Master-Data Enrichment Layer
Decorates canonical DocmateInvoiceV1 JSON payload with customer/company specific BC settings:
- BC Vendor Number
- Default G/L Account
- Dimensions
- Posting Groups
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class CustomerEnrichmentEngine:
    """Enriches canonical invoice JSON with company-specific Business Central master data."""

    def __init__(self, mapping_config: Optional[Dict[str, Any]] = None):
        """
        mapping_config structure:
        {
          "suppliers": {
            "booker wholesale": {
              "bcVendorNo": "V10042",
              "bcGlAccount": "6110",
              "dimensions": {"DEPARTMENT": "ADM", "STORE": "S01"},
              "vatBusPostingGroup": "DOMESTIC"
            }
          },
          "defaults": {
            "bcGlAccount": "6000",
            "currency": "GBP"
          }
        }
        """
        self.mapping_config = mapping_config or {"suppliers": {}, "defaults": {}}

    def enrich(self, canonical_invoice: Dict[str, Any]) -> Dict[str, Any]:
        """Enrich canonical invoice dictionary with BC customer master metadata."""
        if not canonical_invoice or "header" not in canonical_invoice:
            raise ValueError("Invalid canonical invoice structure")
            
        enriched = json_copy(canonical_invoice)
        header = enriched.get("header", {})
        supplier_obj = header.get("supplier", {})
        raw_supplier_name = (supplier_obj.get("rawName") or "").strip().lower()

        # Find customer config match
        suppliers_config = self.mapping_config.get("suppliers", {})
        match_cfg = None

        if raw_supplier_name in suppliers_config:
            match_cfg = suppliers_config[raw_supplier_name]
        elif raw_supplier_name:
            for supp_key, cfg in suppliers_config.items():
                if supp_key in raw_supplier_name or raw_supplier_name in supp_key:
                    match_cfg = cfg
                    break

        defaults = self.mapping_config.get("defaults", {})

        # Populate header BC master data fields
        bc_vendor_no = (match_cfg.get("bcVendorNo") if match_cfg else None) or defaults.get("bcVendorNo") or ""
        bc_gl_account = (match_cfg.get("bcGlAccount") if match_cfg else None) or defaults.get("bcGlAccount") or ""
        dimensions = (match_cfg.get("dimensions") if match_cfg else None) or defaults.get("dimensions") or {}
        vat_bus_posting_group = (match_cfg.get("vatBusPostingGroup") if match_cfg else None) or defaults.get("vatBusPostingGroup") or ""

        header["bcIntegration"] = {
            "bcVendorNo": bc_vendor_no,
            "bcGlAccount": bc_gl_account,
            "dimensions": dimensions,
            "vatBusPostingGroup": vat_bus_posting_group
        }

        # Enrich line items with default BC G/L Account or line dimensions if missing
        lines = enriched.get("lines", [])
        for line in lines:
            if "bcIntegration" not in line:
                line["bcIntegration"] = {
                    "bcGlAccount": bc_gl_account,
                    "dimensions": dimensions
                }

        return enriched

def json_copy(data: Dict[str, Any]) -> Dict[str, Any]:
    """Helper to perform clean deep copy of JSON serializable dict."""
    import json
    return json.loads(json.dumps(data))
