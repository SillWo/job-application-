"""Regression tests for multi-site session concurrency contracts."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.adapters.registry import AdapterRegistry
from backend.intelligence.gateway import ModelGateway
from backend.orchestrator.workflow import WorkflowManager
from backend.schemas.domain import SessionStatus


def test_registry_returns_fresh_adapters_and_isolates_mutable_state():
    registry = AdapterRegistry()

    hh_a = registry.get("hh")
    hh_b = registry.get("hh")
    hirehi_a = registry.get("hirehi")
    hirehi_b = registry.get("hirehi")

    assert hh_a is not hh_b
    assert hirehi_a is not hirehi_b
    assert hh_a.site_id == "hh" and hirehi_a.site_id == "hirehi"

    hh_a._search_page_number = 7
    hh_a._search_seen_ids = {"a"}
    hirehi_a._seen = {"h-a"}
    hirehi_a._category = "аналитика"

    assert not hasattr(hh_b, "_search_page_number")
    assert not hasattr(hh_b, "_search_seen_ids")
    assert not hasattr(hirehi_b, "_seen")
    assert not hasattr(hirehi_b, "_category")


@pytest.mark.asyncio
async def test_model_gateway_instances_can_enter_api_concurrently(monkeypatch):
    entered: list[str] = []
    gate = asyncio.Event()
    release = asyncio.Event()

    class Response:
        def __init__(self, value):
            self.choices = [SimpleNamespace(message=SimpleNamespace(content=value))]

    class Completions:
        async def create(self, *, messages, **kwargs):
            payload = messages[-1]["content"]
            entered.append(payload)
            if len(entered) == 2:
                gate.set()
            await release.wait()
            assessment = '{"score":0,"confidence":0,"evidence":[],"explanation":"нет"}'
            return Response('{"tasks":' + assessment + ',"skills":[],'
                            '"skills_summary":"Навыки соответствуют требованиям вакансии.",'
                            '"experience_depth":' + assessment + ',"role_match":' + assessment + ','
                            '"industry":' + assessment + ','
                            '"special_requirements":' + assessment + ','
                            '"reason":"Вакансия соответствует профилю кандидата."}')

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr("backend.intelligence.gateway.AsyncOpenAI", FakeOpenAI)
    saved = SimpleNamespace(
        base_url="https://api.example.test/v1",
        model="test-model",
        encrypted_api_key="ciphertext",
    )
    monkeypatch.setattr(ModelGateway, "_saved_config", staticmethod(lambda: saved))
    monkeypatch.setattr("backend.intelligence.gateway.decrypt_secret", lambda _: "test-key")

    # Distinct instances must not share a process-wide lock.
    a, b = ModelGateway("openai_compat"), ModelGateway("openai_compat")
    schema = __import__("backend.schemas.domain", fromlist=["ResumeAnalysis"]).ResumeAnalysis
    tasks = [
        asyncio.create_task(a.structured("resume_analyst", {"session": "a"}, schema)),
        asyncio.create_task(b.structured("resume_analyst", {"session": "b"}, schema)),
    ]
    await asyncio.wait_for(gate.wait(), timeout=1)
    assert any('"session": "a"' in item for item in entered)
    assert any('"session": "b"' in item for item in entered)
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_workflow_site_lease_allows_cross_site_blocks_same_site_and_cleans_up(monkeypatch):
    manager = WorkflowManager()
    started = []
    release = asyncio.Event()

    sessions = {
        1: SimpleNamespace(adapter_id="hh", status=SessionStatus.RUNNING),
        2: SimpleNamespace(adapter_id="hirehi", status=SessionStatus.RUNNING),
        3: SimpleNamespace(adapter_id="hh", status=SessionStatus.RUNNING),
    }

    class FakeDB:
        def get(self, model, session_id):
            return sessions.get(session_id)

        def scalar(self, statement):
            return None

    class FakeSessionLocal:
        def __enter__(self):
            return FakeDB()

        def __exit__(self, *args):
            return False

    import backend.orchestrator.workflow as workflow_module

    monkeypatch.setattr(workflow_module, "SessionLocal", FakeSessionLocal)

    async def fake_run(session_id):
        started.append(session_id)
        await release.wait()

    manager._run = fake_run
    assert manager.launch(1) is True
    assert manager.launch(2) is True
    assert manager.launch(3) is False
    await asyncio.sleep(0)
    assert sorted(started) == [1, 2]

    release.set()
    await asyncio.gather(*manager.tasks.values())
    await asyncio.sleep(0)
    assert not manager.site_leases
    assert manager.launch(3) is True
    release.set()
    await manager.tasks[3]


@pytest.mark.asyncio
async def test_workflow_launch_rejects_db_reported_active_same_site(monkeypatch):
    """A persisted PAUSED/WAITING/RUNNING row must reserve its site too."""
    session = SimpleNamespace(adapter_id="hh")
    active = SimpleNamespace(id=99, adapter_id="hh")

    class FakeDB:
        def get(self, model, ident):
            return session if ident == 1 else None

        def scalar(self, statement):
            return active

    class FakeSessionLocal:
        def __enter__(self):
            return FakeDB()

        def __exit__(self, *args):
            return False

    import backend.orchestrator.workflow as workflow_module

    monkeypatch.setattr(workflow_module, "SessionLocal", FakeSessionLocal)
    manager = WorkflowManager()
    assert manager.launch(1) is False
    assert not manager.site_leases
