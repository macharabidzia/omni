import asyncio
import base64

from fastapi.testclient import TestClient

import src.realtime.session as session_module
from src.main import app
from src.realtime.qwen_client import QwenEvent


def _valid_audio_base64() -> str:
    return base64.b64encode(b"\x00\x00" * 1280).decode("ascii")


class FailingQwenClient:
    def __init__(self, **_kwargs) -> None:
        pass

    async def connect(self) -> None:
        raise RuntimeError("unreachable")

    async def close(self) -> None:
        return None


class FakeQwenClient:
    def __init__(self, **_kwargs) -> None:
        self.events: asyncio.Queue[QwenEvent | None] = asyncio.Queue()
        self.audio_chunks: list[str] = []
        self.cancelled = False

    async def connect(self) -> None:
        return None

    async def start_session(
        self,
        *,
        speaker: str,
        modalities: list[str],
        input_sample_rate: int,
        output_audio: bool,
    ) -> None:
        self.speaker = speaker
        self.modalities = modalities
        self.input_sample_rate = input_sample_rate
        self.output_audio = output_audio

    async def append_audio(self, pcm16_base64: str) -> None:
        self.audio_chunks.append(pcm16_base64)

    async def commit_audio(self) -> None:
        await self.events.put(
            QwenEvent(kind="transcript_delta", payload={"type": "response.audio_transcript.delta"}, text="hello ")
        )
        await self.events.put(
            QwenEvent(kind="assistant_text_delta", payload={"type": "response.text.delta"}, text="hi there")
        )
        await self.events.put(
            QwenEvent(
                kind="assistant_audio_delta",
                payload={"type": "response.audio.delta"},
                audio_base64=self.audio_chunks[-1],
                sample_rate=24000,
            )
        )
        await self.events.put(QwenEvent(kind="response_done", payload={"type": "response.done"}))
        await self.events.put(None)

    async def cancel_response(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        return None

    async def iter_events(self):
        while True:
            event = await self.events.get()
            if event is None:
                return
            yield event


def test_websocket_returns_structured_error_when_qwen_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FailingQwenClient)
    client = TestClient(app)

    with client.websocket_connect("/ws/realtime") as websocket:
        websocket.send_json(
            {
                "type": "session.start",
                "speaker": "Ethan",
                "modalities": ["text", "audio"],
                "input_sample_rate": 16000,
                "output_audio": True,
            }
        )

        message = websocket.receive_json()
        assert message["type"] == "error"
        assert message["code"] == "QWEN_UNAVAILABLE"


def test_websocket_streams_normalized_events(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeQwenClient)
    client = TestClient(app)

    with client.websocket_connect("/ws/realtime") as websocket:
        websocket.send_json(
            {
                "type": "session.start",
                "speaker": "Ethan",
                "modalities": ["text", "audio"],
                "input_sample_rate": 16000,
                "output_audio": True,
            }
        )
        first = websocket.receive_json()
        second = websocket.receive_json()
        assert first["type"] == "session.ready"
        assert second["type"] == "metrics.update"

        websocket.send_json(
            {
                "type": "audio.append",
                "audio_base64": _valid_audio_base64(),
                "sample_rate": 16000,
                "channels": 1,
                "format": "pcm16",
            }
        )
        websocket.receive_json()

        websocket.send_json({"type": "audio.commit"})

        seen_types: list[str] = []
        seen_audio_sample_rate = None
        while "assistant.done" not in seen_types:
            event = websocket.receive_json()
            seen_types.append(event["type"])
            if event["type"] == "assistant.audio.delta":
                seen_audio_sample_rate = event["sample_rate"]

        assert "transcript.delta" in seen_types
        assert "assistant.text.delta" in seen_types
        assert "assistant.audio.delta" in seen_types
        assert seen_audio_sample_rate == 24000
