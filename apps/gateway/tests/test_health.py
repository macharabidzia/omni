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
    observed = {"deep": None}

    async def fake_probe_qwen(_settings, *, deep: bool = False):
        observed["deep"] = deep
        return {
            "status": "qwen_ready",
            "detail": "ready",
            "qwen_health_url": "http://qwen/health",
            "qwen_realtime_url": "ws://qwen/v1/realtime",
        }

    async def fake_probe_livekit(_settings):
        return {
            "status": "livekit_ready",
            "detail": "ready",
            "livekit_url": "ws://livekit:7880",
            "livekit_room": "omni-room",
            "livekit_worker_status": "connected",
            "livekit_room_joined": True,
            "livekit_output_track_status": "ready",
        }

    monkeypatch.setattr(health_module, "probe_qwen", fake_probe_qwen)
    monkeypatch.setattr(health_module, "probe_livekit", fake_probe_livekit)
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert observed["deep"] is True


def test_ready_returns_503_when_probe_reports_unreachable(monkeypatch) -> None:
    async def fake_probe_qwen(_settings, *, deep: bool = False):
        return {
            "status": "qwen_unreachable",
            "detail": "boom",
            "qwen_health_url": "http://qwen/health",
            "qwen_realtime_url": "ws://qwen/v1/realtime",
        }

    async def fake_probe_livekit(_settings):
        return {
            "status": "livekit_ready",
            "detail": "ready",
            "livekit_url": "ws://livekit:7880",
            "livekit_room": "omni-room",
            "livekit_worker_status": "connected",
            "livekit_room_joined": True,
            "livekit_output_track_status": "ready",
        }

    monkeypatch.setattr(health_module, "probe_qwen", fake_probe_qwen)
    monkeypatch.setattr(health_module, "probe_livekit", fake_probe_livekit)
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "failed"


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
            ),
            deep=True,
        )
    )

    assert result["status"] == "qwen_failed"
    assert "unsupported" in result["detail"]


def test_probe_qwen_reports_failed_when_realtime_inference_probe_rejects(monkeypatch) -> None:
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
        return None

    async def fake_probe_inference(_settings) -> None:
        raise RuntimeError("timed out waiting for assistant audio")

    monkeypatch.setattr(health_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        health_module,
        "probe_qwen_realtime_websocket",
        fake_probe_realtime,
    )
    monkeypatch.setattr(
        health_module,
        "probe_qwen_realtime_inference",
        fake_probe_inference,
    )

    result = asyncio.run(
        health_module.probe_qwen(
            Settings(
                QWEN_HEALTH_URL="http://qwen/health",
                QWEN_REALTIME_URL="ws://qwen/v1/realtime",
            ),
            deep=True,
        )
    )

    assert result["status"] == "qwen_failed"
    assert "assistant audio" in result["detail"]


def test_probe_qwen_explicit_non_deep_skips_realtime_inference_probe(monkeypatch) -> None:
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
        return None

    async def fail_if_called(_settings) -> None:
        raise AssertionError("deep inference probe should not run for default /ready")

    monkeypatch.setattr(health_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        health_module,
        "probe_qwen_realtime_websocket",
        fake_probe_realtime,
    )
    monkeypatch.setattr(
        health_module,
        "probe_qwen_realtime_inference",
        fail_if_called,
    )

    result = asyncio.run(
        health_module.probe_qwen(
            Settings(
                QWEN_HEALTH_URL="http://qwen/health",
                QWEN_REALTIME_URL="ws://qwen/v1/realtime",
            ),
            deep=False,
        )
    )

    assert result["status"] == "qwen_ready"
    assert "health and realtime session probes" in result["detail"]


def test_probe_qwen_reuses_recent_successful_inference_probe(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_probe_inference(_settings) -> None:
        calls["count"] += 1

    monkeypatch.setattr(health_module, "probe_qwen_realtime_inference", fake_probe_inference)
    monkeypatch.setattr(health_module, "_last_qwen_inference_probe_ok_at", None)

    settings = Settings(
        QWEN_HEALTH_URL="http://qwen/health",
        QWEN_REALTIME_URL="ws://qwen/v1/realtime",
    )

    first = asyncio.run(health_module.probe_qwen_realtime_inference_cached(settings))
    monkeypatch.setattr(
        health_module,
        "_last_qwen_inference_probe_ok_at",
        health_module.time.monotonic(),
    )
    second = asyncio.run(health_module.probe_qwen_realtime_inference_cached(settings))

    assert first is None
    assert second is None
    assert calls["count"] == 1
