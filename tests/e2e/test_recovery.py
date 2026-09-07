"""Real Chromium against a local fixture site; never contacts a job service."""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.adapters.hh.adapter import HHAdapter
from backend.browser import sessions as browsers
from backend.browser.executor import BrowserExecutor
from backend.orchestrator import workflow
from backend.persistence.database import Base
from backend.persistence.models import Application, CandidateProfile, JobSession, Resume
from backend.schemas.domain import JobEvaluation, SessionStatus


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chromium_recovers_page_model_and_lost_submission_response(tmp_path, monkeypatch):
    state = {"reads": 0, "submissions": 0, "models": 0, "browsers": 0}
    account = '<a data-qa="mainmenu_applicantProfile">Account</a>'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/vacancy/1"):
                state["reads"] += 1
                if state["reads"] == 1:
                    body = "Service temporarily unavailable"
                else:
                    button = ('<a data-qa="vacancy-response-link-view-topic">Sent</a>'
                              if state["submissions"] else
                              '<form method="post" action="/apply"><button data-qa="vacancy-response-link-top">Apply</button></form>')
                    body = (account + '<h1 data-qa="vacancy-title">Engineer</h1>'
                            '<div data-qa="vacancy-company-name">Fixture company</div>'
                            '<div data-qa="vacancy-description">Fixture job description</div>' + button)
            else:
                body = account + '<a href="/vacancy/1" data-qa="serp-item__title">Engineer</a>'
            content = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            assert self.path == "/apply"
            state["submissions"] += 1
            # The site accepted the response, but the browser lost its reply.
            self.connection.shutdown(2)
            self.connection.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    class Adapter(HHAdapter):
        site_id = "recovery-e2e"
        allowed_domains = ("127.0.0.1",)
        search_exhausted = True

        async def open_search(self, page, filters):
            await page.goto(base_url)

        async def collect_job_refs(self, page):
            return await self._visible_job_refs(page, timeout=1000)

        async def collect_more_job_refs(self, page):
            return []

        async def extract_job(self, page):
            posting = await super().extract_job(page)
            return posting.model_copy(update={"source": self.site_id})

    # Put all browser storage and SQLite files in the isolated test directory.
    monkeypatch.chdir(tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        profile = CandidateProfile(full_name="Fixture")
        db.add(profile)
        db.flush()
        db.add(Resume(profile_id=profile.id, name="Fixture resume", selected_for_matching=True))
        item = JobSession(profile_id=profile.id, adapter_id=Adapter.site_id, application_limit=1)
        db.add(item)
        db.commit()
        session_id = item.id

    async def restore(ident, adapter):
        state["browsers"] += 1
        assert browsers.acquire_browser_lease(ident, adapter.site_id)
        executor = BrowserExecutor(adapter.site_id, adapter.allowed_domains, headless=True)
        await executor.start()
        browsers.set_browser(ident, executor)
        await executor.execute("navigate", url=base_url)
        return executor

    async def evaluate(*args, **kwargs):
        state["models"] += 1
        if state["models"] == 1:
            raise workflow.ModelUnavailable("fixture outage")
        return JobEvaluation(decision="apply", score=90, confidence=1, category="fixture", reason="fixture")

    async def letter(*args, **kwargs):
        return "Fixture letter"

    async def plan(*args, **kwargs):
        return ["fixture"]

    monkeypatch.setattr(workflow, "SessionLocal", factory)
    monkeypatch.setattr(workflow, "restore_browser", restore)
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: Adapter())
    monkeypatch.setattr(workflow, "evaluate", evaluate)
    monkeypatch.setattr(workflow, "write_cover_letter", letter)
    monkeypatch.setattr(workflow, "plan_search_queries", plan)
    monkeypatch.setattr(workflow, "ModelGateway", lambda: object())
    manager = workflow.WorkflowManager()
    manager.retry_base_seconds = 0.01
    try:
        await asyncio.wait_for(manager.run(session_id), timeout=60)
        with factory() as db:
            item = db.get(JobSession, session_id)
            assert item.status == SessionStatus.COMPLETED
            assert item.counters["submitted"] == item.counters["viewed"] == 1
            assert len(list(db.scalars(select(Application)))) == 1
        assert state["submissions"] == 1
        assert state["models"] == 2
        assert state["browsers"] >= 2
    finally:
        await browsers.close_browser(session_id)
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        engine.dispose()
