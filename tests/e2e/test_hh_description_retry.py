"""Real Chromium against synthetic HH markup, without external navigation."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.adapters.base.errors import JobDescriptionUnavailable
from backend.adapters.base.protocol import JobRef
from backend.adapters.hh.adapter import HHAdapter
from backend.browser.executor import BrowserExecutor
from backend.orchestrator import workflow
from backend.persistence.database import Base
from backend.persistence.models import JobSession


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'description-e2e.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        item = JobSession(adapter_id="hh", status="RUNNING", counters={})
        db.add(item)
        db.commit()
        session_id = item.id
    monkeypatch.setattr(workflow, "SessionLocal", sessions)
    try:
        yield sessions, session_id
    finally:
        engine.dispose()


def _html(description: str = "") -> str:
    return (
        '<h1 data-qa="vacancy-title">Fixture engineer</h1>'
        '<div data-qa="vacancy-company-name">Fixture company</div>'
        f'<div data-qa="vacancy-description">{description}</div>'
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chromium_hh_description_arriving_on_third_read(runtime, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    sessions, session_id = runtime
    executor = BrowserExecutor("hh-description-e2e", ("127.0.0.1",), headless=True)
    adapter = HHAdapter()
    ref = JobRef(external_id="fixture", url="https://hh.ru/vacancy/fixture")
    reads = 0
    reopens = 0
    original_extract = adapter.extract_job

    async def counted_extract(page):
        nonlocal reads
        reads += 1
        return await original_extract(page)

    async def local_open_job(page, _ref):
        nonlocal reopens
        reopens += 1
        if reopens == 2:
            await page.set_content(_html("Fixture description loaded"))
        else:
            await page.set_content(_html())

    adapter.extract_job = counted_extract
    adapter.open_job = local_open_job
    try:
        page = await executor.start()
        await page.set_content(_html())
        monkeypatch.setattr(workflow.WorkflowManager, "description_retry_seconds", 0)
        posting = await workflow.WorkflowManager()._extract_job_with_description_retries(
            session_id, adapter, page, ref
        )
        assert reads == 3
        assert reopens == 2
        assert posting.description == "Fixture description loaded"
        with sessions() as db:
            assert "description_read_attempts" not in db.get(JobSession, session_id).recovery
    finally:
        await executor.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chromium_hh_consistently_missing_description_raises_after_three_reads(
    runtime, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    sessions, session_id = runtime
    executor = BrowserExecutor("hh-description-missing-e2e", ("127.0.0.1",), headless=True)
    adapter = HHAdapter()
    ref = JobRef(external_id="fixture", url="https://hh.ru/vacancy/fixture")
    reads = 0
    reopens = 0
    original_extract = adapter.extract_job

    async def counted_extract(page):
        nonlocal reads
        reads += 1
        return await original_extract(page)

    async def local_open_job(page, _ref):
        nonlocal reopens
        reopens += 1
        await page.set_content(_html())

    adapter.extract_job = counted_extract
    adapter.open_job = local_open_job
    try:
        page = await executor.start()
        await page.set_content(_html())
        monkeypatch.setattr(workflow.WorkflowManager, "description_retry_seconds", 0)
        with pytest.raises(JobDescriptionUnavailable):
            await workflow.WorkflowManager()._extract_job_with_description_retries(
                session_id, adapter, page, ref
            )
        assert reads == 3
        assert reopens == 2
        with sessions() as db:
            assert db.get(JobSession, session_id).recovery["description_read_attempts"] == {
                "fixture": 3
            }
    finally:
        await executor.close()
