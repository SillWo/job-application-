"""End-to-end adaptive HireHi workflow against a local HTML fixture."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.adapters.base.protocol import ApplicationRoute, EmployerContact, JobRef, LoginState
from backend.adapters.hirehi.adapter import HireHiAdapter
from backend.browser.executor import BrowserExecutor
from backend.intelligence.hirehi_adaptive_planner import (
    HireHiPortfolio,
    SearchProfile,
    SearchSource,
)
from backend.orchestrator import hirehi_adaptive_search, workflow
from backend.persistence.database import Base
from backend.persistence.models import (
    Application,
    CoverLetter,
    Evaluation,
    JobSession,
    Vacancy,
)
from backend.schemas.domain import JobEvaluation, JobPosting, SessionStatus
from backend.services.resume_session import _normalize_extracted, persist_session_snapshot


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_hirehi_adaptive_workflow_local_chromium_checkpoint_and_stop(tmp_path, monkeypatch):
    """Run, restart from the persisted adaptive checkpoint, and stop explicitly."""

    requests: list[str] = []
    auth_checks: list[str] = []
    report_calls: list[tuple[int, list[dict]]] = []
    planner_calls = 0
    extract_started = asyncio.Event()
    restored = asyncio.Event()
    restore_errors: list[str] = []
    hold_extraction = False

    def listing(ids: list[str]) -> str:
        cards = "".join(
            f'<a data-vacancy data-id="{ident}" data-title="{title}" '
            f'href="/vacancy/{ident}">{title}</a>'
            for ident, title in ids
        )
        return f'<main data-auth-marker="authenticated">{cards}</main>'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802 - stdlib handler API
            requests.append(self.path)
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                body = '<main data-auth-marker="authenticated">HireHi fixture</main>'
            elif parsed.path == "/search":
                page = query.get("page", ["0"])[0]
                body = listing(
                    [("h-1", "Data Engineer"), ("h-2", "Sales Manager")]
                    if page == "0"
                    else []
                )
            elif parsed.path.startswith("/vacancy/"):
                ident = parsed.path.rsplit("/", 1)[1]
                title = "Data Engineer" if ident == "h-1" else "Sales Manager"
                body = (
                    '<main data-auth-marker="authenticated">'
                    f"<h1>{title}</h1><article>Python data platform work</article>"
                    "</main>"
                )
            else:
                self.send_error(404)
                return
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    class LocalHireHiAdapter(HireHiAdapter):
        """Production HireHi adapter with only local fixture I/O substituted."""

        home_url = base_url + "/"
        allowed_domains = ("127.0.0.1",)

        async def get_login_state(self, page):
            auth_checks.append(page.url)
            authenticated = await page.locator('[data-auth-marker="authenticated"]').count()
            return LoginState(authenticated=bool(authenticated), message="local fixture")

        async def build_source(self, spec):
            raw = dict(spec)
            if raw.get("family") not in {None, "query", "related"}:
                raise ValueError("unexpected fixture source")
            return {**raw, "source_id": "fixture-source", "url": f"{base_url}/search"}

        async def open_source(self, page, spec, cursor=0):
            await page.goto(f"{spec['url']}?page={int(cursor)}")
            self._last_discovery_result = {
                "terminal": int(cursor) > 0,
                "repeated": False,
                "unavailable": False,
                "sources": [],
            }

        async def collect_card_refs(self, page):
            result = []
            cards = page.locator("[data-vacancy]")
            for index in range(await cards.count()):
                card = cards.nth(index)
                result.append(
                    {
                        "external_id": await card.get_attribute("data-id"),
                        "url": f"{base_url}{await card.get_attribute('href')}",
                        "title": await card.get_attribute("data-title"),
                        "description": "Python data platform work",
                        "company": "Fixture Labs",
                    }
                )
            return result

        def discovery_result(self):
            return dict(getattr(self, "_last_discovery_result", {}))

        async def open_job(self, page, ref: JobRef):
            await page.goto(ref.url)
            await page.locator("h1").wait_for(state="visible")

        async def extract_job(self, page):
            nonlocal hold_extraction
            title = (await page.locator("h1").inner_text()).strip()
            if title == "Sales Manager":
                await asyncio.sleep(0.2)
            if hold_extraction and title == "Sales Manager":
                extract_started.set()
                await asyncio.Future()
            return JobPosting(
                source=self.site_id,
                external_id=urlparse(page.url).path.rsplit("/", 1)[1],
                url=page.url,
                title=title,
                company="Fixture Labs",
                description="Python data platform work",
                requires_cover_letter=True,
            )

        async def collect_application_route(self, _page):
            return ApplicationRoute(
                kind="direct_contact",
                contact=EmployerContact(email="hiring@fixture.example"),
            )

        async def collect_visible_sources(self, _page, _context):
            return []

        async def collect_related_refs(self, _page):
            return []

    # The source planner is still invoked by the production adaptive engine,
    # but its model boundary is deterministic and local to this test.
    async def plan_portfolio(*_args, **_kwargs):
        nonlocal planner_calls
        planner_calls += 1
        return HireHiPortfolio(
            profile=SearchProfile(target_roles=["Data Engineer"]),
            sources=[SearchSource(source_id="fixture-source", family="query", query="data")],
        )

    async def evaluate(posting, *_args, **_kwargs):
        decision = "apply" if posting.external_id == "h-1" else "skip"
        return JobEvaluation(
            decision=decision,
            score=95 if decision == "apply" else 10,
            confidence=1,
            category="fixture",
            reason="deterministic fixture verdict",
        )

    async def cover_letter(*_args, **_kwargs):
        return "Fixture cover letter for the data platform role."

    class DeterministicGateway:
        async def structured(self, role, _payload, schema):
            assert role == "job_summary"
            return schema(summary="Fixture summary")

    original_restore = hirehi_adaptive_search.HireHiAdaptiveSearch.restore_search_checkpoint

    async def observe_restore(self, checkpoint):
        try:
            await original_restore(self, checkpoint)
        except Exception as exc:
            restore_errors.append(f"{type(exc).__name__}: {exc}")
            restored.set()
            raise
        restored.set()

    monkeypatch.setattr(
        hirehi_adaptive_search.HireHiAdaptiveSearch,
        "restore_search_checkpoint",
        observe_restore,
    )
    monkeypatch.setattr(hirehi_adaptive_search, "plan_hirehi_portfolio", plan_portfolio)
    monkeypatch.setattr(workflow, "ModelGateway", DeterministicGateway)
    monkeypatch.setattr(workflow, "evaluate", evaluate)
    monkeypatch.setattr(workflow, "write_cover_letter", cover_letter)
    monkeypatch.setattr(
        workflow,
        "write_session_pdf",
        lambda session_id, rows: report_calls.append((session_id, rows)) or "fixture.pdf",
    )
    monkeypatch.chdir(tmp_path)

    engine = create_engine(f"sqlite:///{tmp_path / 'hirehi-adaptive.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    executor = BrowserExecutor("hirehi-adaptive-fixture", ("127.0.0.1",), headless=True)
    manager = workflow.WorkflowManager()

    async def restore_browser(_session_id, _adapter):
        return executor

    async def close_browser(_session_id):
        return None

    monkeypatch.setattr(workflow, "SessionLocal", factory)
    monkeypatch.setattr(workflow, "restore_browser", restore_browser)
    monkeypatch.setattr(workflow, "close_browser", close_browser)
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: LocalHireHiAdapter())

    resume_snapshot = _normalize_extracted(
        {
            "external_id": "fixture-resume",
            "identity": {"full_name": "Fixture", "gender": "male"},
            "target": {"title": "Data Engineer"},
            "about": "Fixture professional background",
            "skills": [{"name": "Python"}],
        },
        adapter_id="hirehi",
        source_url="https://hirehi.ru/resume/fixture-resume",
    )

    def add_session():
        with factory() as db:
            item = JobSession(adapter_id="hirehi", application_limit=None)
            db.add(item)
            db.flush()
            persist_session_snapshot(db, item.id, resume_snapshot)
            db.commit()
            return item.id

    session_id = add_session()
    try:
        await executor.start()
        await executor.page.goto(base_url + "/")
        assert await executor.page.locator('[data-auth-marker="authenticated"]').count() == 1

        run_task = asyncio.create_task(manager.run(session_id))
        manager.tasks[session_id] = run_task
        for _ in range(300):
            with factory() as db:
                item = db.get(JobSession, session_id)
                checkpoint = (item.recovery or {}).get("search_checkpoint")
                ready = (
                    item.counters.get("reported") == 1
                    and checkpoint
                    and len(checkpoint.get("seen_exact_ids", [])) == 2
                    and len(checkpoint.get("analyzed_ids", [])) >= 1
                    and len(checkpoint.get("relevant_ids", [])) == 1
                )
            if ready:
                break
            await asyncio.sleep(0.02)
        assert ready
        # Keep the second card in a real browser operation while the endpoint
        # performs its status transition and task cancellation.
        hold_extraction = True
        for _ in range(100):
            if extract_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert extract_started.is_set()

        # Simulate a worker crash after durable progress.  Cancellation alone
        # leaves the RUNNING row and its snapshot/checkpoint intact; only the
        # subsequent explicit STOP below takes the production terminal path.
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        assert run_task.done()
        assert run_task.cancelled()
        with factory() as db:
            item = db.get(JobSession, session_id)
            checkpoint = (item.recovery or {}).get("search_checkpoint")
            assert item.status == SessionStatus.RUNNING
            assert item.counters["reported"] == 1
            assert len(checkpoint["seen_exact_ids"]) == 2
            assert len(checkpoint["analyzed_ids"]) >= 1
            assert len(checkpoint["relevant_ids"]) == 1
            assert auth_checks
            assert any(path.startswith("/search?") for path in requests)
            assert db.scalar(select(func.count()).select_from(Application)) == 0
            assert db.scalar(select(func.count()).select_from(Vacancy)) == 1
            assert db.scalar(select(func.count()).select_from(Evaluation)) == 1
            assert db.scalar(select(func.count()).select_from(CoverLetter)) == 1
            reported = db.scalar(
                select(Vacancy).where(Vacancy.external_id == "h-1", Vacancy.session_id == session_id)
            )
            assert reported is not None and reported.state == "REPORTED"
        checkpoint_metrics = {
            key: checkpoint[key]
            for key in ("seen_exact_ids", "analyzed_ids", "relevant_ids")
        }

        # Simulate a worker restart using the same nonterminal durable row and
        # its persisted checkpoint.
        hold_extraction = False
        extract_started.clear()
        restored.clear()
        restart_task = asyncio.create_task(manager.run(session_id))
        manager.tasks[session_id] = restart_task
        for _ in range(100):
            if restored.is_set() or restart_task.done():
                break
            await asyncio.sleep(0.05)
        if restart_task.done() and not restored.is_set():
            restart_task.result()
        assert not restore_errors, restore_errors
        assert restored.is_set(), "restart did not reach checkpoint restore"
        # A valid checkpoint must be restored before open_search; restarting
        # must not invoke the fresh portfolio planner again.
        assert planner_calls == 1
        from backend.api import router

        monkeypatch.setattr(router, "workflow_manager", manager)
        monkeypatch.setattr(router, "close_browser", close_browser)
        with factory() as db:
            stopped = await router.stop_session(session_id, db)
        assert stopped["status"] == SessionStatus.CANCELLED
        assert restart_task.done()
        assert restart_task.cancelled()
        with factory() as db:
            item = db.get(JobSession, session_id)
            checkpoint_after_restart = (item.recovery or {}).get("search_checkpoint")
            assert item.status == SessionStatus.CANCELLED
            assert {
                key: checkpoint_after_restart[key]
                for key in checkpoint_metrics
            } == checkpoint_metrics
        with factory() as db:
            item = db.get(JobSession, session_id)
            assert item.status == SessionStatus.CANCELLED
            assert db.scalar(select(func.count()).select_from(Vacancy)) == 1
            assert db.scalar(select(func.count()).select_from(Evaluation)) == 1
            assert db.scalar(select(func.count()).select_from(Application)) == 0
        assert sum(call[0] == session_id for call in report_calls) >= 1
    finally:
        hold_extraction = False
        for task in (locals().get("run_task"), locals().get("restart_task")):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await executor.close()
        engine.dispose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
