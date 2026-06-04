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
    last_instance = None

    def __init__(self, **_kwargs) -> None:
        type(self).last_instance = self
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


class FakeEarlyAssistantQwenClient(FakeQwenClient):
    async def append_audio(self, pcm16_base64: str) -> None:
        await super().append_audio(pcm16_base64)
        if len(self.audio_chunks) != 1:
            return
        await self.events.put(
            QwenEvent(kind="transcript_delta", payload={"type": "response.audio_transcript.delta"}, text="hello ")
        )
        await self.events.put(
            QwenEvent(
                kind="assistant_audio_delta",
                payload={"type": "response.audio.delta"},
                audio_base64=pcm16_base64,
                sample_rate=24000,
            )
        )

    async def commit_audio(self) -> None:
        await self.events.put(
            QwenEvent(kind="assistant_text_delta", payload={"type": "response.text.delta"}, text="hi there")
        )
        await self.events.put(QwenEvent(kind="response_done", payload={"type": "response.done"}))
        await self.events.put(None)


class FakeRestartableQwenClient(FakeQwenClient):
    instances: list["FakeRestartableQwenClient"] = []

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.instance_id = len(type(self).instances) + 1
        self.closed = False
        type(self).instances.append(self)

    async def commit_audio(self) -> None:
        await self.events.put(
            QwenEvent(
                kind="assistant_audio_delta",
                payload={"type": "response.audio.delta"},
                audio_base64=self.audio_chunks[-1],
                sample_rate=24000,
            )
        )

    async def close(self) -> None:
        self.closed = True
        await self.events.put(None)


class FakeQwenChatClient:
    def __init__(self, **_kwargs) -> None:
        self.requests: list[bytes] = []
        self.closed = False

    async def respond_text_only(self, *, audio_pcm16: bytes, sample_rate: int) -> str:
        self.requests.append(audio_pcm16)
        assert sample_rate == 16000
        return "brief text reply"

    async def close(self) -> None:
        self.closed = True


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


def test_websocket_streams_audio_upstream_but_buffers_assistant_output_until_commit(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeEarlyAssistantQwenClient)
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
        assert websocket.receive_json()["type"] == "session.ready"
        assert websocket.receive_json()["type"] == "metrics.update"

        websocket.send_json(
            {
                "type": "audio.append",
                "audio_base64": _valid_audio_base64(),
                "sample_rate": 16000,
                "channels": 1,
                "format": "pcm16",
            }
        )

        precommit_events = [websocket.receive_json(), websocket.receive_json(), websocket.receive_json()]
        assert [event["type"] for event in precommit_events] == [
            "metrics.update",
            "transcript.delta",
            "metrics.update",
        ]
        assert FakeEarlyAssistantQwenClient.last_instance.audio_chunks == [_valid_audio_base64()]

        websocket.send_json({"type": "audio.commit"})

        seen_types: list[str] = []
        while "assistant.done" not in seen_types:
            event = websocket.receive_json()
            seen_types.append(event["type"])

        assert "assistant.audio.delta" in seen_types
        assert "assistant.text.delta" in seen_types
        assert seen_types.index("assistant.audio.delta") < seen_types.index("assistant.done")


def test_websocket_text_mode_uses_chat_completions(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FailingQwenClient)
    monkeypatch.setattr(session_module, "QwenChatClient", FakeQwenChatClient)
    client = TestClient(app)

    with client.websocket_connect("/ws/realtime") as websocket:
        websocket.send_json(
            {
                "type": "session.start",
                "speaker": "Ethan",
                "modalities": ["text"],
                "input_sample_rate": 16000,
                "output_audio": False,
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
        seen_text = None
        while "assistant.done" not in seen_types:
            event = websocket.receive_json()
            seen_types.append(event["type"])
            if event["type"] == "assistant.text.delta":
                seen_text = event["text"]

        assert "assistant.audio.delta" not in seen_types
        assert seen_text == "brief text reply"


def test_websocket_cancel_restarts_upstream_qwen_session(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeRestartableQwenClient)
    FakeRestartableQwenClient.instances = []
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
        assert websocket.receive_json()["type"] == "session.ready"
        assert websocket.receive_json()["type"] == "metrics.update"

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

        first_audio = None
        while first_audio is None:
            event = websocket.receive_json()
            if event["type"] == "assistant.audio.delta":
                first_audio = event

        websocket.send_json({"type": "response.cancel"})
        cancel_event = websocket.receive_json()
        assert cancel_event["type"] == "metrics.update"

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

        second_audio = None
        while second_audio is None:
            event = websocket.receive_json()
            if event["type"] == "assistant.audio.delta":
                second_audio = event

    assert len(FakeRestartableQwenClient.instances) == 2
    assert FakeRestartableQwenClient.instances[0].closed is True
    assert FakeRestartableQwenClient.instances[1].closed is True
    assert first_audio["sample_rate"] == 24000
    assert second_audio["sample_rate"] == 24000
