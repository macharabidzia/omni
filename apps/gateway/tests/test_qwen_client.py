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


def test_parse_event_treats_transcription_done_as_terminal_response() -> None:
    client = QwenRealtimeClient(
        model="qwen",
        url="ws://qwen/v1/realtime",
        request_timeout_seconds=1.0,
        response_timeout_seconds=1.0,
        output_sample_rate=24000,
        max_ws_message_bytes=1024,
    )
    client.awaiting_response = True

    event = client._parse_event({"type": "transcription.done"})
    assert event is not None
    assert event.kind == "response_done"
    assert client.awaiting_response is False

    assert client._parse_event({"type": "response.audio.done"}) is None


def test_qwen_client_starts_generation_only_on_explicit_commit() -> None:
    websocket = FakeWebSocket([])
    client = QwenRealtimeClient(
        model="qwen",
        url="ws://qwen/v1/realtime",
        request_timeout_seconds=1.0,
        response_timeout_seconds=1.0,
        output_sample_rate=24000,
        max_ws_message_bytes=1024,
    )
    client.websocket = websocket

    asyncio.run(client.append_audio("aGVsbG8="))
    asyncio.run(client.commit_audio())

    sent_payloads = [json.loads(payload) for payload in websocket.sent]
    assert sent_payloads == [
        {"type": "input_audio_buffer.append", "audio": "aGVsbG8="},
        {"type": "input_audio_buffer.commit", "final": False},
        {"type": "input_audio_buffer.commit", "final": True},
    ]
