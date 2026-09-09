"""Exercise the real HireHi adapter and workflow against an isolated HTML site."""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.adapters.base.protocol import LoginState
from backend.adapters.hirehi.adapter import HireHiAdapter
from backend.browser.executor import BrowserExecutor
from backend.intelligence.hirehi_category import HireHiCategoryChoice
from backend.orchestrator import workflow
from backend.persistence.database import Base
from backend.persistence.models import BrowserEvent, CandidateProfile, JobSession, Resume, Vacancy
from backend.schemas.domain import JobEvaluation, SessionStatus


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_hirehi_category_pagination_and_recovery(tmp_path, monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            page_number = int(query.get("page", ["1"])[0])
            requests.append(self.path)
            if parsed.path.startswith("/management/job-"):
                body = '<main><h1>Fixture role</h1><article>Fixture description</article></main>'
            else:
                # The category click is accepted by the UI without navigation.
                # Grade selection also loses the category path, as a stale UI can.
                body = '''<input aria-label="Компания, должность или +навык">
                    <button onclick="document.getElementById('categories').hidden=false">
                        Категория все вакансии 4</button>
                    <div id="categories" role="dialog" aria-label="Категория" hidden>
                      <a href="/vacancies/management" onclick="event.preventDefault()">менеджмент</a>
                    </div>
                    <div class="filter-group"><span class="filter-title">грейд</span>
                    <div class="filter-chip" onclick="location.href='/?level=intern'">
                      <span class="chip-text">intern</span>
                    </div></div>'''
                if query.get("level"):
                    body = body.replace('class="filter-chip"', 'class="filter-chip active"')
                start = 1 if page_number == 1 else 3
                body += "<main>" + "".join(
                    f'<a href="/management/job-{i}">Fixture job {i}</a>'
                    for i in range(start, start + 2)
                ) + f'</main><a href="/blog/article-{page_number + 100}">Article</a>'
            content = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/"
    outage = {"raised": False}

    class Adapter(HireHiAdapter):
        home_url = base_url
        allowed_domains = ("127.0.0.1",)

        async def get_login_state(self, page):
            return LoginState(authenticated=True, message="fixture")

        async def collect_more_job_refs(self, page):
            if not outage["raised"]:
                outage["raised"] = True
                raise TimeoutError("fixture listing outage")
            return await super().collect_more_job_refs(page)

    monkeypatch.chdir(tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'hirehi.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        profile = CandidateProfile(full_name="Fixture")
        db.add(profile)
        db.flush()
        db.add(Resume(profile_id=profile.id, name="Fixture", selected_for_matching=True))
        item = JobSession(profile_id=profile.id, adapter_id="hirehi", application_limit=None)
        db.add(item)
        db.commit()
        session_id = item.id

    async def category(*args):
        return HireHiCategoryChoice(category="менеджмент", reason="fixture")

    async def evaluate(*args, **kwargs):
        return JobEvaluation(decision="skip", score=10, confidence=1, category="fixture", reason="fixture")

    executor = BrowserExecutor("hirehi-fixture", Adapter.allowed_domains, headless=True)
    monkeypatch.setattr(workflow, "SessionLocal", factory)
    monkeypatch.setattr(workflow, "get_browser", lambda _: SimpleNamespace(page=executor.page))
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: Adapter())
    monkeypatch.setattr(workflow, "choose_hirehi_category", category)
    monkeypatch.setattr(workflow, "hirehi_grades", lambda _: (0, ["intern"]))
    monkeypatch.setattr(workflow, "ModelGateway", lambda: object())
    monkeypatch.setattr(workflow, "evaluate", evaluate)
    monkeypatch.setattr(workflow, "write_session_pdf", lambda *args: None)
    manager = workflow.WorkflowManager()
    manager.retry_base_seconds = 0
    try:
        await executor.start()
        await asyncio.wait_for(manager.run(session_id), timeout=60)
        with factory() as db:
            item = db.get(JobSession, session_id)
            assert item.status == SessionStatus.COMPLETED
            assert item.counters["viewed"] == item.counters["filtered"] == 4
            assert item.recovery["pending_refs"] == []
            assert item.recovery["search_checkpoint"]["exhausted"]
            vacancies = list(db.scalars(select(Vacancy)))
            assert {v.external_id for v in vacancies} == {"1", "2", "3", "4"}
            assert all(v.state == "REJECTED_BY_MODEL" for v in vacancies)
            retries = list(db.scalars(select(BrowserEvent).where(BrowserEvent.event_type == "recovery_retry")))
            assert len(retries) == 1
        assert requests.index("/management/job-1") < requests.index("/vacancies/management?level=intern&page=2")
        assert requests.count("/vacancies/management?level=intern&page=2") == 1
        assert requests.count("/vacancies/management?level=intern&page=3") == 1
        assert not any("page=4" in path or path.startswith("/blog/") for path in requests)
    finally:
        await executor.close()
        engine.dispose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
