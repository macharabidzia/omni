from fastapi.testclient import TestClient

import src.health as health_module
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
