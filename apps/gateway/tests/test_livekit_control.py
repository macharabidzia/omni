import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

import src.livekit.control as control_module
from src.config import Settings
from src.livekit.auth import build_browser_identity, build_browser_token, build_worker_token
from src.main import app


def _decode_jwt_payload(token: str) -> dict[str, object]:
    payload_segment = token.split(".")[1]
    payload_segment += "=" * (-len(payload_segment) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_segment))


def test_build_browser_identity_uses_configured_prefix() -> None:
    identity = build_browser_identity(Settings(LIVEKIT_BROWSER_IDENTITY_PREFIX="web"))

    assert identity.startswith("web-")
    assert len(identity) > len("web-")


def test_livekit_tokens_use_minimal_expected_video_grants() -> None:
    settings = Settings(
        LIVEKIT_API_KEY="api-key",
        LIVEKIT_API_SECRET="0123456789abcdef0123456789abcdef",
        LIVEKIT_ROOM="omni-room",
        LIVEKIT_AGENT_ID="omni-worker",
    )

    browser_payload = _decode_jwt_payload(
        build_browser_token(settings, identity="browser-test")
    )
    worker_payload = _decode_jwt_payload(build_worker_token(settings))

    assert browser_payload["sub"] == "browser-test"
    assert worker_payload["sub"] == "omni-worker"
    assert browser_payload["video"] == {
        "roomJoin": True,
        "room": "omni-room",
        "canPublish": True,
        "canSubscribe": True,
        "canPublishData": True,
    }
    assert worker_payload["video"] == {
        "roomJoin": True,
        "room": "omni-room",
        "canPublish": True,
        "canSubscribe": True,
        "canPublishData": True,
    }


def test_livekit_session_returns_503_when_control_plane_is_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(control_module, "get_settings", lambda: Settings())
    client = TestClient(app)

    response = client.post("/livekit/session")

    assert response.status_code == 503
    assert response.json() == {"detail": "LiveKit control plane is not configured."}


def test_livekit_session_returns_identity_token_and_topic(monkeypatch) -> None:
    settings = Settings(
        LIVEKIT_URL="ws://185.62.58.164:7880",
        LIVEKIT_API_KEY="api-key",
        LIVEKIT_API_SECRET="0123456789abcdef0123456789abcdef",
        LIVEKIT_ROOM="omni-room",
        LIVEKIT_CONTROL_TOPIC="omni.control",
        LIVEKIT_BROWSER_IDENTITY_PREFIX="browser",
    )
    monkeypatch.setattr(control_module, "get_settings", lambda: settings)
    client = TestClient(app)

    response = client.post("/livekit/session")

    assert response.status_code == 200
    payload = response.json()
    assert payload["livekit_url"] == "ws://185.62.58.164:7880"
    assert payload["room"] == "omni-room"
    assert payload["control_topic"] == "omni.control"
    assert payload["participant_identity"].startswith("browser-")

    token_payload = _decode_jwt_payload(payload["participant_token"])
    assert token_payload["sub"] == payload["participant_identity"]
    assert token_payload["video"]["room"] == "omni-room"


def test_livekit_session_returns_503_when_worker_is_draining(monkeypatch, tmp_path: Path) -> None:
    snapshot_path = tmp_path / "livekit-worker-state.json"
    snapshot_path.write_text(
        json.dumps({"livekit_worker_status": "draining"}),
        encoding="utf-8",
    )
    settings = Settings(
        LIVEKIT_URL="ws://185.62.58.164:7880",
        LIVEKIT_API_KEY="api-key",
        LIVEKIT_API_SECRET="0123456789abcdef0123456789abcdef",
        LIVEKIT_ROOM="omni-room",
        LIVEKIT_WORKER_STATE_PATH=str(snapshot_path),
    )
    monkeypatch.setattr(control_module, "get_settings", lambda: settings)
    client = TestClient(app)

    response = client.post("/livekit/session")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "LiveKit worker is draining and not accepting new sessions."
    }
