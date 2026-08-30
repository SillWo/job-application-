import asyncio
import base64
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api import router as api
from backend.intelligence import gateway
from backend.persistence import crypto
from backend.persistence.database import Base
from backend.persistence.models import AIModelSettings


@pytest.fixture()
def app_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_db] = lambda: (yield from _session(Session))
    monkeypatch.setattr(api, "validate_base_url", lambda value, _allowed=(): value.rstrip("/"))
    return app, Session


def _session(factory):
    with factory() as db:
        yield db


def test_dpapi_roundtrip_and_ciphertext(monkeypatch):
    monkeypatch.setattr(crypto.sys, "platform", "win32")
    monkeypatch.setattr(
        crypto, "_protect", lambda data: base64.b64encode(b"cipher:" + data).decode()
    )
    monkeypatch.setattr(crypto, "_unprotect", lambda value: base64.b64decode(value)[7:])
    encrypted = crypto.encrypt_secret("secret-value")
    assert encrypted != "secret-value"
    assert crypto.decrypt_secret(encrypted) == "secret-value"


def test_corrupt_ciphertext(monkeypatch):
    monkeypatch.setattr(crypto.sys, "platform", "win32")
    with pytest.raises(ValueError):
        crypto.decrypt_secret("not-base64!")


def test_get_settings_defaults_never_exposes_secret(app_db, monkeypatch):
    app, _ = app_db
    monkeypatch.setattr(api.settings, "openai_base_url", "https://default.example/v1")
    monkeypatch.setattr(api.settings, "openai_model", "default-model")
    response = TestClient(app).get("/api/model/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["base_url"] == "https://default.example/v1"
    assert body["model"] == "default-model"
    assert "encrypted_api_key" not in body and "secret-value" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_list_models_uses_supplied_bearer_and_hides_key(app_db, monkeypatch):
    app, _ = app_db
    seen = {}

    async def models(base, key):
        seen.update(base=base, key=key)
        return ["z-model", "a-model"]

    monkeypatch.setattr(api, "_models", models)
    response = TestClient(app).post(
        "/api/model/models", json={"base_url": "https://api.example/v1", "api_key": "top-secret"}
    )
    assert response.status_code == 200
    assert seen["key"] == "top-secret"
    assert "top-secret" not in response.text


def test_save_then_blank_key_preserves_ciphertext(app_db, monkeypatch):
    app, Session = app_db
    monkeypatch.setattr(api, "encrypt_secret", lambda key: "encrypted:" + key)
    monkeypatch.setattr(api, "decrypt_secret", lambda value: value.removeprefix("encrypted:"))
    monkeypatch.setattr(api, "_models", lambda base, key: asyncio.sleep(0, result=["model-a"]))
    client = TestClient(app)
    assert (
        client.put(
            "/api/model/settings",
            json={"base_url": "https://api.example/v1", "model": "model-a", "api_key": "key"},
        ).status_code
        == 200
    )
    with Session() as db:
        first = db.get(AIModelSettings, 1).encrypted_api_key
    assert (
        client.put(
            "/api/model/settings",
            json={"base_url": "https://api.example/v1", "model": "model-a", "api_key": ""},
        ).status_code
        == 200
    )
    with Session() as db:
        assert db.get(AIModelSettings, 1).encrypted_api_key == first


def test_unavailable_model_rolls_back(app_db, monkeypatch):
    app, Session = app_db
    monkeypatch.setattr(api, "encrypt_secret", lambda key: "encrypted:" + key)
    monkeypatch.setattr(api, "_models", lambda base, key: asyncio.sleep(0, result=["other"]))
    response = TestClient(app).put(
        "/api/model/settings",
        json={"base_url": "https://api.example/v1", "model": "missing", "api_key": "key"},
    )
    assert response.status_code == 400
    with Session() as db:
        assert db.get(AIModelSettings, 1) is None


def test_cross_site_origin_forbidden(app_db):
    app, _ = app_db
    response = TestClient(app).post(
        "/api/model/models",
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        json={"base_url": "https://api.example/v1", "api_key": "x"},
    )
    assert response.status_code == 403


def test_private_url_rejected():
    from backend.intelligence.model_config import validate_base_url

    with pytest.raises(ValueError):
        validate_base_url("https://localhost/v1")


def test_only_configured_loopback_gateway_allows_local_http():
    from backend.intelligence.model_config import validate_base_url

    url = "http://127.0.0.1:8045/v1"
    assert validate_base_url(url, (url,)) == url
    with pytest.raises(ValueError):
        validate_base_url("http://127.0.0.1:8046/v1", (url,))


def test_gateway_loads_persisted_config(monkeypatch):
    item = SimpleNamespace(
        base_url="https://saved.example/v1", model="saved-model", encrypted_api_key="cipher"
    )
    monkeypatch.setattr(gateway.ModelGateway, "_saved_config", staticmethod(lambda: item))
    monkeypatch.setattr(gateway, "decrypt_secret", lambda value: "saved-key")
    captured = {}

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(gateway, "AsyncOpenAI", Client)
    monkeypatch.setattr(gateway.settings, "llm_provider", "openai_compat")
    gw = gateway.ModelGateway()

    class Response:
        choices = [SimpleNamespace(message=SimpleNamespace(content='{"summary":"ok"}'))]

    class Completions:
        async def create(self, **kwargs):
            captured["request"] = kwargs
            return Response()

    class Client(Client):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr(gateway, "AsyncOpenAI", Client)
    from backend.intelligence.hirehi_category import JobSummary

    result = asyncio.run(gw._structured_openai("job_summary", {"job": {"title": "x"}}, JobSummary))
    assert result.summary == "ok"
    assert captured["base_url"] == item.base_url
    assert captured["api_key"] == "saved-key"
    assert captured["request"]["model"] == item.model


def test_gateway_status_sanitizes_corrupt_dpapi_secret(monkeypatch):
    item = SimpleNamespace(
        base_url="https://saved.example/v1", model="saved-model", encrypted_api_key="broken"
    )
    monkeypatch.setattr(gateway.ModelGateway, "_saved_config", staticmethod(lambda: item))
    monkeypatch.setattr(
        gateway,
        "decrypt_secret",
        lambda _value: (_ for _ in ()).throw(RuntimeError("sensitive DPAPI detail")),
    )

    result = asyncio.run(gateway.ModelGateway(provider="openai_compat").status())

    assert result == {
        "connected": False,
        "model_available": False,
        "provider": "openai_compat",
        "model": "saved-model",
        "message": "Не удалось подключиться к модели",
    }
    assert "sensitive" not in str(result)


def test_env_key_is_not_settings_field(monkeypatch):
    monkeypatch.setenv("JAO_OPENAI_API_KEY", "must-not-load")
    import backend.config as config

    assert not hasattr(config.settings, "openai_api_key")


def test_project_dotenv_is_ignored(tmp_path, monkeypatch):
    """A local .env must not be an implicit configuration source."""
    (tmp_path / ".env").write_text(
        "JAO_OPENAI_MODEL=dotenv-model\nJAO_OPENAI_BASE_URL=https://dotenv.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from backend.config import Settings

    loaded = Settings()
    assert loaded.openai_model == "gpt-4o-mini"
    assert loaded.openai_base_url == "https://api.openai.com/v1"


def test_gateway_uses_saved_ui_model_settings(monkeypatch):
    """Gateway requests always use the encrypted, UI-persisted config."""
    item = SimpleNamespace(
        base_url="https://saved.example/v1", model="saved-model", encrypted_api_key="cipher"
    )
    monkeypatch.setattr(gateway.ModelGateway, "_saved_config", staticmethod(lambda: item))
    monkeypatch.setattr(gateway, "decrypt_secret", lambda value: "saved-key")
    captured = {}

    class Completions:
        async def create(self, **kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content='{"summary":"ok"}'))
                ]
            )

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr(gateway, "AsyncOpenAI", Client)
    from backend.intelligence.hirehi_category import JobSummary

    result = asyncio.run(
        gateway.ModelGateway(provider="openai_compat")._structured_openai(
            "job_summary", {"job": {"title": "x"}}, JobSummary
        )
    )
    assert result.summary == "ok"
    assert captured["base_url"] == item.base_url
    assert captured["api_key"] == "saved-key"
    assert captured["request"]["model"] == item.model
