from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient


def _load_stub_server_module():
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / "scripts" / "stub_qwen_server.py"
    spec = importlib.util.spec_from_file_location("stub_qwen_server", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_stub_qwen_health_endpoint_reports_ready() -> None:
    module = _load_stub_server_module()
    client = TestClient(module.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["backend"] == "stub_qwen"


def test_stub_qwen_realtime_endpoint_emits_audio_done_sequence() -> None:
    module = _load_stub_server_module()
    client = TestClient(module.app)

    with client.websocket_connect("/v1/realtime") as websocket:
        assert websocket.receive_json()["type"] == "session.created"
        websocket.send_text(json.dumps({"type": "session.update", "model": "stub"}))
        websocket.send_text(json.dumps({"type": "input_audio_buffer.commit", "final": False}))
        websocket.send_text(json.dumps({"type": "input_audio_buffer.append", "audio": "AAAA"}))
        websocket.send_text(json.dumps({"type": "input_audio_buffer.commit", "final": True}))

        transcript = websocket.receive_json()
        text = websocket.receive_json()
        audio = websocket.receive_json()
        done = websocket.receive_json()

    assert transcript["type"] == "response.audio_transcript.delta"
    assert text["type"] == "response.text.delta"
    assert audio["type"] == "response.audio.delta"
    assert isinstance(audio["delta"], str) and audio["delta"]
    assert done["type"] == "response.done"


def test_stub_qwen_chat_stream_endpoint_emits_text_and_audio_chunks() -> None:
    module = _load_stub_server_module()
    client = TestClient(module.app)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "stub",
            "modalities": ["text", "audio"],
            "stream": True,
        },
    ) as response:
        lines = [line for line in response.iter_lines() if line]

    decoded_lines = [
        line.decode("utf-8") if isinstance(line, bytes) else line
        for line in lines
    ]

    assert response.status_code == 200
    assert any('"modality": "text"' in line for line in decoded_lines)
    assert any('"modality": "audio"' in line for line in decoded_lines)
    assert decoded_lines[-1] == "data: [DONE]"
