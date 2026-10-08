from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from backend.api import router as api
from backend.persistence.database import Base
from backend.persistence.models import BrowserEvent, JobSession


@pytest.fixture
def event_api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'events.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(JobSession(id=1, adapter_id="hh", status="COMPLETED", counters={}))
        db.commit()
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for offset in range(0, 61_834, 1000):
            db.add_all([
                BrowserEvent(
                    session_id=1,
                    event_type="progress",
                    message=f"event-{index + 1}",
                    data={"ordinal": index + 1},
                    created_at=timestamp,
                )
                for index in range(offset, min(offset + 1000, 61_834))
            ])
            db.commit()

    app = FastAPI()

    def get_test_db():
        with factory() as db:
            yield db

    app.include_router(api.router)
    app.dependency_overrides[api.get_db] = get_test_db
    app.websocket("/ws/sessions/{session_id}")(api.session_socket)
    from backend.persistence import database
    monkeypatch.setattr(database, "SessionLocal", factory)
    yield app, engine, factory
    engine.dispose()


def test_events_page_sql_and_cursors_bound_large_history(event_api):
    app, engine, _factory = event_api
    statements: list[str] = []
    sqlalchemy_event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
    )
    with TestClient(app) as client:
        first = client.get("/api/sessions/1/events")
        assert first.status_code == 200
        assert len(first.json()) == 500
        assert first.json()[0]["id"] == 1
        assert first.json()[-1]["id"] == 500
        assert any("LIMIT" in statement.upper() for statement in statements)

        assert client.get("/api/sessions/1/events", params={"after": -1}).status_code == 422
        assert client.get("/api/sessions/1/events", params={"limit": 1001}).status_code == 422

        ids = []
        cursor = 0
        while True:
            page = client.get(
                "/api/sessions/1/events",
                params={"after": cursor, "limit": 1000},
            ).json()
            if not page:
                break
            page_ids = [row["id"] for row in page]
            assert page_ids == sorted(set(page_ids))
            ids.extend(page_ids)
            cursor = page_ids[-1]
        assert ids == list(range(1, 61_835))


def test_websocket_reconnects_from_supplied_cursor_without_replay(event_api):
    app, _engine, factory = event_api
    with TestClient(app) as client:
        with client.websocket_connect("/ws/sessions/1?after=61830") as socket:
            first = [socket.receive_json() for _ in range(4)]
        assert [row["id"] for row in first] == [61831, 61832, 61833, 61834]
        with factory() as db:
            db.add(BrowserEvent(
                session_id=1,
                event_type="progress",
                message="new event",
                data={"ordinal": 61835},
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ))
            db.commit()
        with client.websocket_connect(f"/ws/sessions/1?after={first[-1]['id']}") as socket:
            event = socket.receive_json()
            assert event["id"] == 61835


def test_websocket_rejects_invalid_cursors(event_api):
    app, _engine, _factory = event_api
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/sessions/1?after=-1") as socket,
        pytest.raises(WebSocketDisconnect),
    ):
        socket.receive_json()
