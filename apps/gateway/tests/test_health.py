import asyncio

from fastapi.testclient import TestClient

import src.health as health_module
from src.config import Settings
from src.main import app


def test_health_returns_container_alive() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "container_alive"}


def test_ready_returns_200_when_probe_reports_ready(monkeypatch) -> None:
    async def fake_probe(_settings):
        return {
            "status": "qwen_ready",
            "detail": "ready",
            "qwen_health_url": "http://qwen/health",
            "qwen_realtime_url": "ws://qwen/v1/realtime",
        }

    monkeypatch.setattr(health_module, "probe_qwen", fake_probe)
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "qwen_ready"


def test_ready_returns_503_when_probe_reports_unreachable(monkeypatch) -> None:
    async def fake_probe(_settings):
        return {
            "status": "qwen_unreachable",
            "detail": "boom",
            "qwen_health_url": "http://qwen/health",
            "qwen_realtime_url": "ws://qwen/v1/realtime",
        }

    monkeypatch.setattr(health_module, "probe_qwen", fake_probe)
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "qwen_unreachable"


def test_probe_qwen_reports_failed_when_realtime_session_probe_rejects(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200

    class FakeAsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, _url: str) -> FakeResponse:
            return FakeResponse()

    async def fake_probe_realtime(**_kwargs) -> None:
        raise RuntimeError("unsupported: realtime endpoint disabled")

    monkeypatch.setattr(health_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        health_module,
        "probe_qwen_realtime_websocket",
        fake_probe_realtime,
    )

    result = asyncio.run(
        health_module.probe_qwen(
            Settings(
                QWEN_HEALTH_URL="http://qwen/health",
                QWEN_REALTIME_URL="ws://qwen/v1/realtime",
            )
        )
    )

    assert result["status"] == "qwen_failed"
    assert "unsupported" in result["detail"]
