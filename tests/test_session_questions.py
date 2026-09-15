from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.router import router


def test_removed_session_question_endpoints_return_404():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/api/sessions/1/questions").status_code == 404
        assert client.post(
            "/api/sessions/1/questions/q1", json={"answer": "Москва"}
        ).status_code == 404
