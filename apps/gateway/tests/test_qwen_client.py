import asyncio
import json

import pytest

import src.realtime.qwen_client as qwen_client
from src.realtime.qwen_client import QwenRealtimeClient, probe_qwen_realtime_websocket


class FakeWebSocket:
    def __init__(self, messages: list[str | bytes]) -> None:
        self.messages = list(messages)
        self.closed = False
        self.sent: list[str] = []

    async def recv(self) -> str | bytes:
        if not self.messages:
            raise AssertionError("No more fake websocket messages queued.")
        return self.messages.pop(0)

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


def test_probe_qwen_realtime_websocket_accepts_session_created(monkeypatch) -> None:
    websocket = FakeWebSocket(
        [json.dumps({"type": "session.created", "id": "sess-1"})]
    )

    async def fake_connect(*_args, **_kwargs):
        return websocket

    monkeypatch.setattr(qwen_client.websockets, "connect", fake_connect)

    asyncio.run(
        probe_qwen_realtime_websocket(
            url="ws://qwen/v1/realtime",
            request_timeout_seconds=1.0,
            max_ws_message_bytes=1024,
        )
    )

    assert websocket.closed is True


def test_qwen_realtime_client_connect_rejects_error_startup_event(monkeypatch) -> None:
    websocket = FakeWebSocket(
        [
            json.dumps(
                {
                    "type": "error",
                    "code": "unsupported",
                    "error": "The /v1/realtime API is not supported.",
                }
            )
        ]
    )

    async def fake_connect(*_args, **_kwargs):
        return websocket

    monkeypatch.setattr(qwen_client.websockets, "connect", fake_connect)

    client = QwenRealtimeClient(
        model="qwen",
        url="ws://qwen/v1/realtime",
        request_timeout_seconds=1.0,
        response_timeout_seconds=1.0,
        output_sample_rate=24000,
        max_ws_message_bytes=1024,
    )

    with pytest.raises(RuntimeError, match="unsupported"):
        asyncio.run(client.connect())

    assert websocket.closed is True


def test_parse_event_uses_string_error_message() -> None:
    client = QwenRealtimeClient(
        model="qwen",
        url="ws://qwen/v1/realtime",
        request_timeout_seconds=1.0,
        response_timeout_seconds=1.0,
        output_sample_rate=24000,
        max_ws_message_bytes=1024,
    )

    event = client._parse_event(
        {
            "type": "error",
            "code": "unsupported",
            "error": "The /v1/realtime API is not supported.",
        }
    )

    assert event is not None
    assert event.code == "unsupported"
    assert event.message == "The /v1/realtime API is not supported."


def test_parse_event_ignores_non_terminal_audio_done_events() -> None:
    client = QwenRealtimeClient(
        model="qwen",
        url="ws://qwen/v1/realtime",
        request_timeout_seconds=1.0,
        response_timeout_seconds=1.0,
        output_sample_rate=24000,
        max_ws_message_bytes=1024,
    )

    assert client._parse_event({"type": "response.audio.done"}) is None
    assert client._parse_event({"type": "transcription.done"}) is None

    event = client._parse_event({"type": "response.done"})
    assert event is not None
    assert event.kind == "response_done"
