import asyncio
import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api import router as api
from backend.intelligence import gateway, model_config
from backend.intelligence.hirehi_category import JobSummary
from backend.intelligence.model_broker import ModelRequestClient, ModelVersions, SubmitRequest
from backend.persistence import crypto
from backend.persistence.database import Base
from backend.persistence.model_request_models import ModelGenerationHealth, ModelRequest
from backend.persistence.models import AIModelSettings, JobSession


@pytest.mark.parametrize("steps", [
    [], ["schema"], ["schema", "json"], ["tokens", "temperature", "schema", "json"],
])
def test_real_sdk_negotiates_explicitly_unsupported_parameters(monkeypatch, steps):
    requests = []
    errors = {
        "schema": ("response_format", "json_schema is not supported"),
        "json": ("response_format", "json_object is not supported"),
        "tokens": ("max_tokens", "max_tokens is unsupported; use max_completion_tokens"),
        "temperature": ("temperature", "Only the default temperature is supported"),
    }

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url == "https://provider.example/custom/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer arbitrary-provider-token"
        assert payload["model"] == "provider/model-name"
        if len(requests) <= len(steps):
            param, message = errors[steps[len(requests) - 1]]
            return httpx.Response(400, json={"error": {"param": param, "message": message}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"result":"ok"}'}}]})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(gateway, "model_http_client", lambda *args: transport)
    asyncio.run(REAL_CHECK_CONNECTION(
        gateway.ModelGateway(provider="openai_compat"),
        "https://provider.example/custom/v1", "arbitrary-provider-token", "provider/model-name",
    ))
    assert transport.is_closed
    assert len(requests) == len(steps) + 1
    last = requests[-1]
    if "schema" in steps:
        assert '"const": "ok"' in last["messages"][0]["content"]
    if "json" in steps:
        assert "response_format" not in last
    if "tokens" in steps:
        assert "max_tokens" not in last and last["max_completion_tokens"] == 128
    if "temperature" in steps:
        assert "temperature" not in last


@pytest.mark.parametrize(("status", "error_type", "error_code"), [
    (400, gateway.ModelPermanentError, "provider_capability_unsupported"),
    (401, gateway.ModelPermanentError, "provider_unauthorized"),
    (403, gateway.ModelPermanentError, "provider_unauthorized"),
    (404, gateway.ModelUnavailable, "provider_unavailable"),
    (429, gateway.ModelUnavailable, "provider_unavailable"),
    (500, gateway.ModelUnavailable, "provider_unavailable"),
])
def test_probe_does_not_retry_other_errors_or_expose_arbitrary_key(
    monkeypatch, status, error_type, error_code,
):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "invalid request arbitrary-secret-123"}})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(gateway, "model_http_client", lambda *args: transport)
    with pytest.raises(error_type) as error:
        asyncio.run(REAL_CHECK_CONNECTION(
            gateway.ModelGateway(provider="openai_compat"), "https://provider.example/v1",
            "arbitrary-secret-123", "model",
        ))
    assert "arbitrary-secret-123" not in str(error.value)
    assert error.value.error_code == error_code
    assert len(requests) == 1
    assert transport.is_closed


def test_probe_rejects_invalid_json_after_fallback(monkeypatch):
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(422, json={"error": {"message": "json_schema not supported"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"result":"wrong"}'}}]})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(gateway, "model_http_client", lambda *args: transport)
    with pytest.raises(gateway.ModelPermanentError):
        asyncio.run(REAL_CHECK_CONNECTION(
            gateway.ModelGateway(provider="openai_compat"), "http://127.0.0.1:8045/v1", "", "model",
        ))
    assert len(requests) == 4
    assert transport.is_closed


def test_changed_endpoint_does_not_receive_saved_key(app_db, monkeypatch):
    app, sessions = app_db
    with sessions() as db:
        db.add(AIModelSettings(id=1, base_url="https://original.example/v1", model="model", encrypted_api_key="cipher"))
        db.commit()

    def unexpected(*args):
        pytest.fail("The old key must not be read for another endpoint")

    monkeypatch.setattr(api, "decrypt_secret", unexpected)
    client = TestClient(app)
    response = client.post("/api/model/models", json={"base_url": "https://new.example/v1"})
    assert response.status_code == 400
    assert "API ключ не задан" in response.text
    response = client.put("/api/model/settings", json={"base_url": "http://127.0.0.1:8045/v1", "model": "manual-model"})
    assert response.status_code == 200
    assert response.json()["has_api_key"] is False
    with sessions() as db:
        assert db.get(AIModelSettings, 1).encrypted_api_key == ""

REAL_CHECK_CONNECTION = gateway.ModelGateway.check_connection


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
    monkeypatch.setattr(gateway.ModelGateway, "check_connection", lambda *args: asyncio.sleep(0))
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


def test_model_status_keeps_catalog_and_real_generation_health_separate(
    app_db, monkeypatch
):
    app, sessions = app_db
    now = datetime.now(timezone.utc)
    with sessions() as db:
        session = JobSession(adapter_id="hh", status="RUNNING", counters={})
        db.add(session)
        db.commit()
        session_id = session.id

    client = ModelRequestClient(sessions)
    versions = ModelVersions(
        model_id="fixture-model",
        model_version="1",
        prompt_version="1",
        schema_version="1",
        parser_version="1",
    )
    success = client.submit(
        SubmitRequest(
            session_id=session_id,
            site_id="hh",
            stage="summary",
            role="job_summary",
            payload={"job": {"title": "safe"}},
            schema=JobSummary,
            versions=versions,
        )
    )
    failure = client.submit(
        SubmitRequest(
            session_id=session_id,
            site_id="hh",
            stage="evaluation",
            role="job_summary",
            payload={"job": {"title": "safe-2"}},
            schema=JobSummary,
            versions=versions,
        )
    )
    with sessions() as db:
        success_row = db.get(ModelRequest, success.request_id)
        success_row.status = "completed"
        success_row.canonical_output = '{"summary":"ok"}'
        success_row.completed_at = now - timedelta(seconds=5)
        failure_row = db.get(ModelRequest, failure.request_id)
        failure_row.status = "failed"
        failure_row.completed_at = now
        failure_row.error_code = "provider-secret-must-not-be-returned"
        db.add(
            ModelGenerationHealth(
                id=1,
                success_count=3,
                failure_count=2,
                last_success_request_id=success.request_id,
                last_success_at=now - timedelta(seconds=5),
                last_failure_request_id=failure.request_id,
                last_failure_at=now,
                last_error_code="provider-secret-must-not-be-returned",
            )
        )
        db.commit()

    async def catalog(_self):
        return {
            "connected": True,
            "model_available": True,
            "provider": "fixture",
            "model": "fixture-model",
            "message": "Каталог доступен",
        }

    monkeypatch.setattr(api.ModelGateway, "status", catalog)
    response = TestClient(app).get("/api/model/status")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["connected"] is True
    assert body["model_available"] is True
    assert body["message"] == "Каталог доступен"
    assert body["generation_health"] == {
        "healthy": False,
        "success_count": 3,
        "failure_count": 2,
        "running": 0,
        "queued": 0,
        "last_success": {
            "diagnostic_id": success.diagnostic_id,
            "at": (now - timedelta(seconds=5)).isoformat(),
        },
        "last_failure": {
            "diagnostic_id": failure.diagnostic_id,
            "at": now.isoformat(),
        },
    }
    assert "provider-secret" not in response.text
    assert success.request_id not in response.text
    assert failure.request_id not in response.text


def test_model_status_reports_unknown_generation_without_inferring_from_catalog(
    app_db, monkeypatch
):
    app, _ = app_db

    async def catalog(_self):
        return {
            "connected": True,
            "model_available": True,
            "provider": "fixture",
            "model": "fixture-model",
        }

    monkeypatch.setattr(api.ModelGateway, "status", catalog)
    response = TestClient(app).get("/api/model/status")

    assert response.status_code == 200
    assert response.json()["generation_health"] == {
        "healthy": None,
        "success_count": 0,
        "failure_count": 0,
        "running": 0,
        "queued": 0,
        "last_success": None,
        "last_failure": None,
    }


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
    async def unavailable(*args):
        raise gateway.ModelUnavailable("Модель недоступна")
    monkeypatch.setattr(gateway.ModelGateway, "check_connection", unavailable)
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


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "0.0.0.0"])
@pytest.mark.parametrize("key", ["fixture-key", ""])
def test_first_setup_contacts_local_model_server(app_db, monkeypatch, host, key):
    app, sessions = app_db
    monkeypatch.setattr(gateway.ModelGateway, "check_connection", REAL_CHECK_CONNECTION)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(api, "validate_base_url", model_config.validate_base_url)
    monkeypatch.setattr(api.settings, "openai_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(api, "encrypt_secret", lambda key: "fixture:" + key)
    monkeypatch.setattr(api, "decrypt_secret", lambda value: value.removeprefix("fixture:"))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization")))
            assert payload["model"] == "fixture-model"
            assert payload["response_format"]["type"] == "json_schema"
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": '{"result":"ok"}'}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
    base = f"http://{host}:{server.server_port}/custom/v1"
    try:
        with TestClient(app) as client:
            response = client.post("/api/model/models", json={"base_url": base + "/", "api_key": key})
            assert response.status_code == 200, response.text
            assert response.json() == {"models": ["fixture-model"]}
            response = client.put("/api/model/settings", json={
                "base_url": base, "api_key": key, "model": "fixture-model",
            })
            assert response.status_code == 200, response.text
            assert response.json()["base_url"] == base.replace("0.0.0.0", "127.0.0.1")
            assert "fixture-key" not in response.text
            assert client.post("/api/model/models", json={"base_url": base}).status_code == 200
        assert requests == [(path, f"Bearer {key}" if key else None) for path in (
            "/custom/v1/models", "/custom/v1/chat/completions", "/custom/v1/models",
        )]
        with sessions() as db:
            saved = db.get(AIModelSettings, 1)
            assert saved.model == "fixture-model"
            monkeypatch.setattr(gateway.ModelGateway, "_saved_config", staticmethod(lambda: saved))
        monkeypatch.setattr(gateway, "decrypt_secret", lambda value: value.removeprefix("fixture:"))
        result = asyncio.run(gateway.ModelGateway(provider="openai_compat")._structured_openai(
            "connection_check", {}, gateway.ConnectionCheck,
        ))
        assert result.result == "ok"
        assert requests[-1] == ("/custom/v1/chat/completions", f"Bearer {key}" if key else None)
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
        payload["base_url"] = "https://8.8.8.8/v1"
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


def test_status_explains_missing_catalog_without_generating(monkeypatch):
    item = SimpleNamespace(base_url="http://127.0.0.1:8045/v1", model="manual-model", encrypted_api_key="")
    monkeypatch.setattr(gateway.ModelGateway, "_saved_config", staticmethod(lambda: item))
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(404)

    monkeypatch.setattr(gateway, "model_http_client", lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    result = asyncio.run(gateway.ModelGateway(provider="openai_compat").status())
    assert result["connected"] and not result["model_available"]
    assert "не предоставляет список" in result["message"]
    assert len(requests) == 1 and requests[0].method == "GET"


def test_model_redirect_does_not_forward_key(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://another.example/v1/models"})

    monkeypatch.setattr(api, "model_http_client", lambda *args: httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=False,
    ))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(api._models("https://provider.example/v1", "fixture-key"))
    assert len(requests) == 1


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
