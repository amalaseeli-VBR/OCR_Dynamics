"""Windows Service host for the standalone Dynamics OCR worker."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil


BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import dynamics_workflow as workflow  # noqa: E402


SERVICE_NAME = "DocMateDynamicsOCR"
SERVICE_DISPLAY_NAME = "DocMate Dynamics OCR Worker"
SERVICE_DESCRIPTION = "Processes pending Dynamics invoice attachments through DocMate OCR."


def _configure_file_logging() -> None:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "dynamics_workflow.log"
    root = logging.getLogger()
    resolved = str(log_path.resolve()).casefold()
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler):
            if str(Path(handler.baseFilename).resolve()).casefold() == resolved:
                return
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class DocMateDynamicsService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.interval_seconds = workflow.DEFAULT_INTERVAL_SECONDS

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        _configure_file_logging()
        servicemanager.LogInfoMsg(f"{SERVICE_DISPLAY_NAME} starting")
        workflow.logger.info("Dynamics Windows service started")
        try:
            config = workflow._read_config()
            while win32event.WaitForSingleObject(self.stop_event, 0) != win32event.WAIT_OBJECT_0:
                try:
                    workflow.process_next(config)
                except Exception:
                    workflow.logger.exception("Dynamics service polling error")
                win32event.WaitForSingleObject(self.stop_event, self.interval_seconds * 1000)
        finally:
            workflow.logger.info("Dynamics Windows service stopped")
            servicemanager.LogInfoMsg(f"{SERVICE_DISPLAY_NAME} stopped")


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(DocMateDynamicsService)
