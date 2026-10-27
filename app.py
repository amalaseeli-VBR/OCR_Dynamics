"""Standalone Streamlit entry point for DocMate v1.18."""

from pathlib import Path
import runpy


runpy.run_path(
    str(Path(__file__).with_name("docmate_app_v1.18.001_r01_customer.py")),
    run_name="__main__",
)

