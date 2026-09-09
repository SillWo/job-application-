"""Full adaptive workflow with real Chromium and a local fixture HH surface."""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.adapters.hh.adapter import HHAdapter
from backend.browser import sessions as browsers
from backend.browser.executor import BrowserExecutor
from backend.orchestrator import workflow
from backend.persistence.database import Base
from backend.persistence.models import Application, CandidateProfile, JobSession, Resume
from backend.schemas.domain import JobEvaluation


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_adaptive_workflow_discovers_ui_channels_without_duplicate_submissions(tmp_path, monkeypatch):
    requests, submitted = [], []
    account = '<a data-qa="mainmenu_applicantProfile">Account</a><a href="/applicant/resumes">Resumes</a>'

    def link(ident):
        return f'<a data-qa="serp-item__title" href="/vacancy/{ident}">Engineer {ident}</a>'

    def listing(*ids):
        return "".join(link(i) for i in ids) + '<div data-qa="pager-block">1</div>'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                body = listing(1) + '<a href="/search/vacancy?resume=fixture">Подходящие вакансии</a>'
            elif parsed.path == "/applicant/resumes":
                body = '<a href="/resume/fixture">Engineer resume</a>'
            elif parsed.path == "/resume/fixture":
                body = '<a href="/search/vacancy?resume=second">Подходящие вакансии</a>'
            elif parsed.path == "/employer/55":
                body = listing(1, 5)
            elif parsed.path == "/search/vacancy":
                if query.get("resume") == ["fixture"]:
                    body = listing(1, 2)
                elif query.get("resume") == ["second"]:
                    body = listing(2, 6)
                elif query.get("area") == ["1"]:
                    body = listing(7)
                elif "text" in query:
                    body = listing(2, 3)
                else:
                    body = listing(3, 4) + '<a href="/search/vacancy?area=1">Region</a>'
            elif parsed.path.startswith("/vacancy/"):
                ident = parsed.path.rsplit("/", 1)[1]
                body = (f'<h1 data-qa="vacancy-title">Engineer {ident}</h1>'
                        '<div data-qa="vacancy-company-name"><a href="/employer/55">Company</a></div>'
                        '<div data-qa="vacancy-description">Fixture engineering tasks</div>'
                        '<form method="post" action="/apply/' + ident + '"><button data-qa="vacancy-response-link-top">Apply</button></form>')
                if ident == "1":
                    body += '<div data-qa="similar-vacancies">' + link(8) + '</div>'
            else:
                self.send_error(404)
                return
            data = (account + body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            ident = self.path.rsplit("/", 1)[1]
            submitted.append(ident)
            data = (account + '<a data-qa="vacancy-response-link-view-topic">Отклик отправлен</a>').encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/"

    class Adapter(HHAdapter):
        home_url = base_url
        allowed_domains = ("127.0.0.1",)

        async def open_search(self, page, filters):
            await page.goto(base_url)

    monkeypatch.chdir(tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'adaptive.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        profile = CandidateProfile(full_name="Fixture")
        db.add(profile)
        db.flush()
        db.add(Resume(profile_id=profile.id, name="Fixture", desired_title="Engineer", selected_for_matching=True))
        item = JobSession(profile_id=profile.id, adapter_id="hh", application_limit=None)
        db.add(item)
        db.commit()
        ident = item.id

    async def restore(session_id, adapter):
        assert browsers.acquire_browser_lease(session_id, "hh")
        executor = BrowserExecutor("hh", adapter.allowed_domains, headless=True)
        await executor.start()
        await executor.page.goto(base_url)
        browsers.set_browser(session_id, executor)
        return executor

    async def plan(*args, **kwargs):
        return [{"query": "Engineer", "field": "name", "cluster": "core"}]

    async def evaluate(*args, **kwargs):
        return JobEvaluation(decision="apply", score=90, confidence=1, category="fixture", reason="fixture")

    async def letter(*args, **kwargs):
        return "Fixture letter"

    monkeypatch.setattr(workflow, "SessionLocal", factory)
    monkeypatch.setattr(workflow, "restore_browser", restore)
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: Adapter())
    monkeypatch.setattr(workflow, "plan_portfolio", plan)
    monkeypatch.setattr(workflow, "evaluate", evaluate)
    monkeypatch.setattr(workflow, "write_cover_letter", letter)
    try:
        await asyncio.wait_for(workflow.WorkflowManager().run(ident), timeout=150)
        with factory() as db:
            item = db.get(JobSession, ident)
            report = item.recovery["measurement_report"]
            assert item.status == "COMPLETED"
            assert report["identity"]["algorithm"] == "adaptive_v1"
            assert report["unique_discovered"] == report["relevant"] == 8
            assert report["unjudged"] == 0
            assert report["duplicate_discoveries"] > 0
            assert len(list(db.scalars(select(Application)))) == 8
            assert item.counters["submitted"] == 8
        assert len(submitted) == len(set(submitted)) == 8
        assert any("area=1" in url for url in requests)
        assert any("resume=second" in url for url in requests)
        assert "/employer/55" in requests
    finally:
        await browsers.close_browser(ident)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        engine.dispose()
