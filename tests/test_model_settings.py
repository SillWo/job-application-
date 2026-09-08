import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api import router as api
from backend.intelligence import gateway, model_config
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
        validate_base_url("https://192.168.1.20/v1")


def test_loopback_gateway_can_be_configured_for_the_first_time():
    from backend.intelligence.model_config import validate_base_url

    url = "http://127.0.0.1:8045/v1"
    assert validate_base_url("http://127.0.0.1/v1") == "http://127.0.0.1/v1"
    assert validate_base_url(url) == url
    assert validate_base_url(url, (url,)) == url
    assert validate_base_url("http://127.0.0.1:8046/v1", (url,)).endswith(":8046/v1")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_first_setup_contacts_local_model_server(app_db, monkeypatch, host):
    app, sessions = app_db
    monkeypatch.setattr(api, "validate_base_url", model_config.validate_base_url)
    monkeypatch.setattr(api.settings, "openai_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(api, "encrypt_secret", lambda key: "fixture:" + key)
    monkeypatch.setattr(api, "decrypt_secret", lambda value: value.removeprefix("fixture:"))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            body = json.dumps({"data": [{"id": "fixture-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{host}:{server.server_port}/v1"
    try:
        with TestClient(app) as client:
            response = client.post("/api/model/models", json={"base_url": base + "/", "api_key": "fixture-key"})
            assert response.status_code == 200, response.text
            assert response.json() == {"models": ["fixture-model"]}
            response = client.put("/api/model/settings", json={
                "base_url": base, "api_key": "fixture-key", "model": "fixture-model",
            })
            assert response.status_code == 200, response.text
            assert response.json()["base_url"] == base
            assert "fixture-key" not in response.text
            assert client.post("/api/model/models", json={"base_url": base}).status_code == 200
        assert requests == [("/v1/models", "Bearer fixture-key")] * 3
        with sessions() as db:
            assert db.get(AIModelSettings, 1).model == "fixture-model"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("host, addresses, accepted", [
    ("localhost", ["127.0.0.1", "::1"], True),
    ("[::1]", ["::1"], True),
    ("localhost", ["127.0.0.1", "192.168.1.20"], False),
    ("outside.example", ["127.0.0.1"], False),
    ("outside.example", [], False),
])
def test_local_url_requires_explicit_loopback_host(monkeypatch, host, addresses, accepted):
    monkeypatch.setattr(model_config.socket, "getaddrinfo", lambda *args, **kwargs: [
        (0, 0, 0, "", (address, 8045)) for address in addresses
    ])
    url = f"http://{host}:8045/v1"
    if accepted:
        assert model_config.validate_base_url(url) == url
    else:
        with pytest.raises(ValueError):
            model_config.validate_base_url(url)


@pytest.mark.parametrize("save", [False, True])
@pytest.mark.parametrize("problem", ["address", "missing_key", "unreadable_key"])
def test_model_setup_reports_local_failure_without_contacting_server(app_db, monkeypatch, save, problem):
    app, sessions = app_db
    monkeypatch.setattr(api, "validate_base_url", model_config.validate_base_url)
    payload = {"base_url": "http://127.0.0.1:8045/v1"}
    if problem == "address":
        payload.update(base_url="http://192.168.1.20/v1", api_key="fixture-key")
        expected = "Адрес модели"
    elif problem == "missing_key":
        expected = "API ключ не задан"
    else:
        with sessions() as db:
            db.add(AIModelSettings(id=1, base_url=payload["base_url"], model="fixture-model", encrypted_api_key="fixture-cipher"))
            db.commit()
        def fail(_value):
            raise RuntimeError("private decryption details")
        monkeypatch.setattr(api, "decrypt_secret", fail)
        expected = "Введите ключ заново"

    async def unexpected_request(*args):
        pytest.fail("Invalid configuration must fail before sending a request")

    monkeypatch.setattr(api, "_models", unexpected_request)
    with TestClient(app) as client:
        if save:
            response = client.put("/api/model/settings", json={**payload, "model": "fixture-model"})
        else:
            response = client.post("/api/model/models", json=payload)
    assert response.status_code == 400
    assert expected in response.json()["detail"]
    assert "private decryption details" not in response.text
    assert "fixture-key" not in response.text


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
