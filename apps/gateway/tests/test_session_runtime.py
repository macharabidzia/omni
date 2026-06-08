import asyncio
import base64

import pytest

import src.realtime.session as session_module
from src.config import Settings
from src.realtime.qwen_client import QwenEvent
from src.realtime.session import RealtimeSession


def _valid_audio_base64() -> str:
    return base64.b64encode(b"\x00\x00" * 1280).decode("ascii")


class EventCollector:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, payload: dict) -> None:
        self.events.append(payload)


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


class FakePrecommitResponseQwenClient(FakeQwenClient):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.commit_calls = 0

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
        await self.events.put(QwenEvent(kind="response_done", payload={"type": "response.done"}))
        await self.events.put(None)

    async def commit_audio(self) -> None:
        self.commit_calls += 1


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


class FakeStaleResponseQwenClient(FakeRestartableQwenClient):
    async def commit_audio(self) -> None:
        await self.events.put(
            QwenEvent(
                kind="assistant_audio_delta",
                payload={"type": "response.audio.delta"},
                audio_base64=self.audio_chunks[-1],
                sample_rate=24000,
            )
        )


class FakeAppendRecoveringQwenClient(FakeRestartableQwenClient):
    async def append_audio(self, pcm16_base64: str) -> None:
        if self.instance_id == 1 and not self.audio_chunks:
            raise RuntimeError("received 1012 (service restart); then sent 1012 (service restart)")
        await super().append_audio(pcm16_base64)

    async def commit_audio(self) -> None:
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


class FakeCommitRecoveringQwenClient(FakeRestartableQwenClient):
    async def commit_audio(self) -> None:
        if self.instance_id == 1:
            raise RuntimeError("received 1012 (service restart); then sent 1012 (service restart)")
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


class FakeReconnectRefusedOnBufferedCommitQwenClient(FakeRestartableQwenClient):
    async def connect(self) -> None:
        if self.instance_id == 2:
            raise ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 17091)")

    async def commit_audio(self) -> None:
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


async def _start_audio_session(collector: EventCollector) -> RealtimeSession:
    session = RealtimeSession(
        settings=Settings(),
        emit_event=collector.emit,
    )
    await session.start_session(
        {
            "speaker": "Ethan",
            "modalities": ["text", "audio"],
            "input_sample_rate": 16000,
            "output_audio": True,
        }
    )
    return session


async def _drain_tasks() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _wait_for_event(collector: EventCollector, event_type: str, *, attempts: int = 20) -> None:
    for _ in range(attempts):
        if any(event["type"] == event_type for event in collector.events):
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Timed out waiting for event type={event_type}")


def test_session_requires_event_sink() -> None:
    with pytest.raises(ValueError, match="event sink"):
        RealtimeSession(settings=Settings(), emit_event=None)


@pytest.mark.asyncio
async def test_session_returns_structured_error_when_qwen_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FailingQwenClient)
    collector = EventCollector()
    session = RealtimeSession(settings=Settings(), emit_event=collector.emit)

    await session.start_session(
        {
            "speaker": "Ethan",
            "modalities": ["text", "audio"],
            "input_sample_rate": 16000,
            "output_audio": True,
        }
    )

    assert collector.events[0]["type"] == "error"
    assert collector.events[0]["code"] == "QWEN_UNAVAILABLE"


@pytest.mark.asyncio
async def test_session_streams_normalized_events(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeQwenClient)
    collector = EventCollector()
    session = await _start_audio_session(collector)

    assert collector.events[0]["type"] == "session.ready"
    assert collector.events[1]["type"] == "metrics.update"

    await session.append_audio_chunk(
        audio_base64=_valid_audio_base64(),
        sample_rate=16000,
    )
    await session.commit_audio()
    await _drain_tasks()

    seen_types = [event["type"] for event in collector.events]
    assert "transcript.delta" in seen_types
    assert "assistant.text.delta" in seen_types
    assert "assistant.audio.delta" in seen_types


@pytest.mark.asyncio
async def test_session_buffers_assistant_output_until_commit(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeEarlyAssistantQwenClient)
    collector = EventCollector()
    session = await _start_audio_session(collector)

    await session.append_audio_chunk(
        audio_base64=_valid_audio_base64(),
        sample_rate=16000,
    )
    await _drain_tasks()

    precommit_types = [event["type"] for event in collector.events]
    assert "transcript.delta" in precommit_types
    assert "assistant.audio.delta" not in precommit_types

    await session.commit_audio()
    await _drain_tasks()

    seen_types = [event["type"] for event in collector.events]
    assert "assistant.audio.delta" in seen_types
    metrics_events = [event for event in collector.events if event["type"] == "metrics.update"]
    assert metrics_events
    final_metrics = metrics_events[-1]["metrics"]
    assert final_metrics["commit_to_first_transcript_ms"] == 0
    assert final_metrics["commit_to_first_audio_delta_ms"] == 0


@pytest.mark.asyncio
async def test_session_does_not_double_commit_when_upstream_response_already_started(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakePrecommitResponseQwenClient)
    collector = EventCollector()
    session = await _start_audio_session(collector)

    await session.append_audio_chunk(
        audio_base64=_valid_audio_base64(),
        sample_rate=16000,
    )
    await _drain_tasks()

    client = FakePrecommitResponseQwenClient.last_instance
    assert client is not None
    assert client.commit_calls == 0

    await session.commit_audio()
    await _drain_tasks()

    assert client.commit_calls == 0
    seen_types = [event["type"] for event in collector.events]
    assert seen_types.count("assistant.audio.delta") == 1
    assert seen_types.count("assistant.done") == 1


@pytest.mark.asyncio
async def test_session_text_mode_uses_chat_completions(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FailingQwenClient)
    monkeypatch.setattr(session_module, "QwenChatClient", FakeQwenChatClient)
    collector = EventCollector()
    session = RealtimeSession(settings=Settings(), emit_event=collector.emit)

    await session.start_session(
        {
            "speaker": "Ethan",
            "modalities": ["text"],
            "input_sample_rate": 16000,
            "output_audio": False,
        }
    )
    await session.append_audio_chunk(
        audio_base64=_valid_audio_base64(),
        sample_rate=16000,
    )
    await session.commit_audio()
    await _drain_tasks()

    seen_types = [event["type"] for event in collector.events]
    assert "assistant.audio.delta" not in seen_types
    assert "assistant.text.delta" in seen_types
    assert collector.events[-1]["type"] == "metrics.update"


@pytest.mark.asyncio
async def test_session_cancel_restarts_upstream_qwen_session(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeRestartableQwenClient)
    FakeRestartableQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    await session.append_audio_chunk(
        audio_base64=_valid_audio_base64(),
        sample_rate=16000,
    )
    await session.commit_audio()
    await _drain_tasks()

    await session.cancel_response()
    await session.append_audio_chunk(
        audio_base64=_valid_audio_base64(),
        sample_rate=16000,
    )
    await session.commit_audio()
    await _drain_tasks()

    assert len(FakeRestartableQwenClient.instances) == 2
    assert FakeRestartableQwenClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_session_new_turn_restarts_stale_upstream_qwen_session(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeStaleResponseQwenClient)
    FakeStaleResponseQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    await session.append_audio_chunk(
        audio_base64=_valid_audio_base64(),
        sample_rate=16000,
    )
    await session.commit_audio()
    await _drain_tasks()

    await session.append_audio_chunk(
        audio_base64=_valid_audio_base64(),
        sample_rate=16000,
    )
    await session.commit_audio()
    await _drain_tasks()

    assert len(FakeStaleResponseQwenClient.instances) == 2
    assert FakeStaleResponseQwenClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_session_recovers_turn_after_qwen_append_restart(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeAppendRecoveringQwenClient)
    FakeAppendRecoveringQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    chunk = _valid_audio_base64()
    await session.append_audio_chunk(
        audio_base64=chunk,
        sample_rate=16000,
    )
    await session.commit_audio()
    await _wait_for_event(collector, "assistant.done")

    assert len(FakeAppendRecoveringQwenClient.instances) == 2
    assert FakeAppendRecoveringQwenClient.instances[0].closed is True
    assert FakeAppendRecoveringQwenClient.instances[1].audio_chunks == [chunk]
    assert not [event for event in collector.events if event["type"] == "error"]
    assert any(event["type"] == "assistant.done" for event in collector.events)


@pytest.mark.asyncio
async def test_session_recovers_turn_after_qwen_commit_restart(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeCommitRecoveringQwenClient)
    FakeCommitRecoveringQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    chunk = _valid_audio_base64()
    await session.append_audio_chunk(
        audio_base64=chunk,
        sample_rate=16000,
    )
    await session.commit_audio()
    await _wait_for_event(collector, "assistant.done")

    assert len(FakeCommitRecoveringQwenClient.instances) == 2
    assert FakeCommitRecoveringQwenClient.instances[0].closed is True
    assert FakeCommitRecoveringQwenClient.instances[1].audio_chunks == [chunk]
    assert not [event for event in collector.events if event["type"] == "error"]
    assert any(event["type"] == "assistant.done" for event in collector.events)


@pytest.mark.asyncio
async def test_session_retries_buffered_commit_after_qwen_connect_refused(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeReconnectRefusedOnBufferedCommitQwenClient)
    FakeReconnectRefusedOnBufferedCommitQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    chunk = _valid_audio_base64()
    await session.append_audio_chunk(
        audio_base64=chunk,
        sample_rate=16000,
    )
    await session._close_qwen_realtime_session()
    await session.commit_audio()
    await _wait_for_event(collector, "assistant.done")

    assert len(FakeReconnectRefusedOnBufferedCommitQwenClient.instances) == 3
    assert FakeReconnectRefusedOnBufferedCommitQwenClient.instances[0].closed is True
    assert FakeReconnectRefusedOnBufferedCommitQwenClient.instances[2].audio_chunks == [chunk]
    assert not [event for event in collector.events if event["type"] == "error"]
