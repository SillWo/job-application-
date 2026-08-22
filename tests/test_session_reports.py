import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.router import session_report_pdf, session_report_status


class FakeDB:
    def __init__(self, item): self.item = item
    def get(self, model, ident): return self.item if self.item and self.item.id == ident else None


def hirehi_db(): return FakeDB(SimpleNamespace(id=7, adapter_id="hirehi"))


def test_report_status_missing_and_non_hirehi():
    with pytest.raises(HTTPException) as missing: session_report_status(7, FakeDB(None))
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as wrong: session_report_status(7, FakeDB(SimpleNamespace(id=7, adapter_id="hh")))
    assert wrong.value.status_code == 400


def test_report_status_not_ready_and_deterministic_ready(monkeypatch):
    tmp_path = Path(".test-session-report-status"); shutil.rmtree(tmp_path, ignore_errors=True); tmp_path.mkdir(); monkeypatch.chdir(tmp_path)
    assert session_report_status(7, hirehi_db()) == {"ready": False, "pdf_url": None}
    path = Path("output/pdf/hirehi-session-7.pdf"); path.parent.mkdir(parents=True); path.write_bytes(b"pdf")
    assert session_report_status(7, hirehi_db()) == {"ready": True, "pdf_url": "/api/sessions/7/report/pdf"}
    monkeypatch.chdir(".."); shutil.rmtree(tmp_path, ignore_errors=True)


def test_report_pdf_missing_and_ready(monkeypatch):
    tmp_path = Path(".test-session-report-pdf"); shutil.rmtree(tmp_path, ignore_errors=True); tmp_path.mkdir(); monkeypatch.chdir(tmp_path)
    with pytest.raises(HTTPException) as missing: session_report_pdf(7, hirehi_db())
    assert missing.value.status_code == 404
    path = Path("output/pdf/hirehi-session-7.pdf"); path.parent.mkdir(parents=True); path.write_bytes(b"pdf")
    response = session_report_pdf(7, hirehi_db())
    assert response.filename == "hirehi-session-7.pdf"
    monkeypatch.chdir(".."); shutil.rmtree(tmp_path, ignore_errors=True)
