import asyncio
import json

from fastapi.testclient import TestClient

import src.main as main_module
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
            "livekit_url": "ws://185.62.58.164:7880",
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
    assert observed["deep"] is False


def test_ready_query_can_enable_deep_probe(monkeypatch) -> None:
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
            "livekit_url": "ws://185.62.58.164:7880",
            "livekit_room": "omni-room",
            "livekit_worker_status": "connected",
            "livekit_room_joined": True,
            "livekit_output_track_status": "ready",
        }

    monkeypatch.setattr(health_module, "probe_qwen", fake_probe_qwen)
    monkeypatch.setattr(health_module, "probe_livekit", fake_probe_livekit)
    client = TestClient(app)

    response = client.get("/ready?deep=true")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert observed["deep"] is True


def test_diagnostics_deep_always_uses_deep_probe(monkeypatch) -> None:
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
            "livekit_url": "ws://185.62.58.164:7880",
            "livekit_room": "omni-room",
            "livekit_worker_status": "connected",
            "livekit_room_joined": True,
            "livekit_output_track_status": "ready",
        }

    monkeypatch.setattr(health_module, "probe_qwen", fake_probe_qwen)
    monkeypatch.setattr(health_module, "probe_livekit", fake_probe_livekit)
    client = TestClient(app)

    response = client.get("/diagnostics/deep")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert observed["deep"] is True


def test_probe_livekit_merges_worker_snapshot_rollups(monkeypatch, tmp_path) -> None:
    class FakeRoomService:
        async def list_rooms(self, _request):
            return type("RoomResponse", (), {"rooms": [type("Room", (), {"name": "omni-room"})()]})()

        async def list_participants(self, _request):
            worker = type(
                "Participant",
                (),
                {
                    "identity": "omni-worker",
                    "tracks": [type("Track", (), {"name": "assistant"})()],
                },
            )()
            return type("ParticipantResponse", (), {"participants": [worker]})()

    class FakeLiveKitAPI:
        def __init__(self, **_kwargs) -> None:
            self.room = FakeRoomService()

        async def aclose(self) -> None:
            return None

    snapshot_path = tmp_path / "livekit-worker-state.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "active_sessions": 1,
                "assistant_queue_depth_ms": 120.0,
                "livekit_input_track_status": "ready",
                "livekit_reconnects": 2,
                "qwen_reconnects": 4,
                "error_count": 3,
                "interruption_count": 5,
                "duplicate_commit_count": 2,
                "stale_output_drop_count": 7,
                "qwen_reconnect_reasons": {
                    "startup": 1,
                    "interrupt": 2,
                    "append_recovery": 1,
                },
                "qwen_last_reconnect_reason": "interrupt",
                "metric_rollups": {
                    "commit_to_first_livekit_egress_last_ms": 210.0,
                    "commit_to_first_livekit_egress_p50_ms": 215.0,
                    "commit_to_first_livekit_egress_p95_ms": 275.0,
                    "commit_to_first_livekit_egress_p99_ms": 320.0,
                },
                "updated_at_epoch_ms": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(health_module.livekit_api, "LiveKitAPI", FakeLiveKitAPI)

    result = asyncio.run(
        health_module.probe_livekit(
            Settings(
                LIVEKIT_URL="ws://185.62.58.164:7880",
                LIVEKIT_API_KEY="key",
                LIVEKIT_API_SECRET="secret",
                LIVEKIT_ROOM="omni-room",
                LIVEKIT_AGENT_ID="omni-worker",
                LIVEKIT_WORKER_STATE_PATH=str(snapshot_path),
            )
        )
    )

    assert result["status"] == "livekit_ready"
    assert result["livekit_input_track_status"] == "ready"
    assert result["active_sessions"] == 1
    assert result["livekit_reconnects"] == 2
    assert result["qwen_reconnects"] == 4
    assert result["qwen_reconnect_reasons"]["interrupt"] == 2
    assert result["qwen_last_reconnect_reason"] == "interrupt"
    assert result["assistant_queue_depth_ms"] == 120.0
    assert result["error_count"] == 3
    assert result["interruption_count"] == 5
    assert result["duplicate_commit_count"] == 2
    assert result["stale_output_drop_count"] == 7
    assert result["last_first_audio_ms"] == 210.0
    assert result["p95_first_audio_ms"] == 275.0
    assert isinstance(result["livekit_worker_snapshot_age_ms"], int)


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
            "livekit_url": "ws://185.62.58.164:7880",
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


def test_ready_returns_503_when_livekit_worker_is_draining(monkeypatch) -> None:
    async def fake_probe_qwen(_settings, *, deep: bool = False):
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
            "livekit_url": "ws://185.62.58.164:7880",
            "livekit_room": "omni-room",
            "livekit_worker_status": "draining",
            "livekit_room_joined": True,
            "livekit_output_track_status": "ready",
        }

    monkeypatch.setattr(health_module, "probe_qwen", fake_probe_qwen)
    monkeypatch.setattr(health_module, "probe_livekit", fake_probe_livekit)
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert "draining" in response.json()["detail"]


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
                QWEN_AUDIO_BACKEND="realtime",
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
                QWEN_AUDIO_BACKEND="realtime",
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
                QWEN_AUDIO_BACKEND="realtime",
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
        QWEN_AUDIO_BACKEND="realtime",
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


def test_prewarm_qwen_startup_retries_until_ready(monkeypatch) -> None:
    observed = {"calls": 0, "sleeps": []}

    async def fake_probe_qwen(_settings, *, deep: bool = False):
        observed["calls"] += 1
        assert deep is True
        if observed["calls"] == 1:
            return {
                "status": "qwen_loading",
                "detail": "warming",
            }
        return {
            "status": "qwen_ready",
            "detail": "ready",
        }

    async def fake_sleep(duration: float) -> None:
        observed["sleeps"].append(duration)

    monkeypatch.setattr(health_module, "probe_qwen", fake_probe_qwen)
    monkeypatch.setattr(health_module.asyncio, "sleep", fake_sleep)

    asyncio.run(
        health_module.prewarm_qwen_startup(
            Settings(
                QWEN_PREWARM_ON_STARTUP=True,
                QWEN_PREWARM_MAX_WAIT_SECONDS=5,
                QWEN_PREWARM_RETRY_INTERVAL_SECONDS=1,
            )
        )
    )

    assert observed["calls"] == 2
    assert observed["sleeps"] == [1]


def test_app_startup_schedules_qwen_prewarm_when_enabled(monkeypatch) -> None:
    observed = {"called": 0}

    async def fake_prewarm(_settings) -> None:
        observed["called"] += 1

    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(QWEN_PREWARM_ON_STARTUP=True))
    monkeypatch.setattr(main_module, "prewarm_qwen_startup", fake_prewarm)

    with TestClient(app):
        pass

    assert observed["called"] == 1
