"""
Docmate Supplier Transformation Registry
Manages supplier-specific JSONata mapping templates and rules.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_RULES_FILE = Path(__file__).resolve().parent.parent / "DocMate_DATA" / "supplier_jsonata_rules.json"

class SupplierRegistry:
    """Registry to load, store, and look up JSONata transformation rules per supplier."""

    def __init__(self, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or DEFAULT_RULES_FILE
        self.rules: Dict[str, str] = {}
        self._load_rules()

    def _load_rules(self) -> None:
        """Load stored supplier rules from JSON storage."""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self.rules = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load supplier JSONata rules from {self.storage_path}: {e}")
                self.rules = {}
        else:
            self.rules = {}

    def save_rules(self) -> None:
        """Persist current supplier rules to JSON storage."""
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(self.rules, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save supplier JSONata rules to {self.storage_path}: {e}")

    def register_rule(self, supplier_alias: str, rule_expression: str) -> None:
        """Register or update a custom JSONata transformation rule for a supplier."""
        clean_alias = supplier_alias.strip().lower()
        self.rules[clean_alias] = rule_expression
        self.save_rules()

    def get_rule(self, supplier_name: str) -> Optional[str]:
        """Look up custom JSONata transformation rule by supplier name alias."""
        if not supplier_name:
            return None
        clean_name = supplier_name.strip().lower()
        
        # Exact match
        if clean_name in self.rules:
            return self.rules[clean_name]
            
        # Substring / fuzzy alias match
        for alias, rule in self.rules.items():
            if alias in clean_name or clean_name in alias:
                return rule
                
        return None
