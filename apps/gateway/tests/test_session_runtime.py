import asyncio
import base64

import pytest
import pytest_asyncio

import src.realtime.session as session_module
from src.config import Settings
from src.realtime.qwen_client import QwenEvent
from src.realtime.session import RealtimeSession, RealtimeSessionConfig


def _valid_audio_base64() -> str:
    return base64.b64encode(b"\x00\x00" * 1280).decode("ascii")


def _audio_base64_for_samples(sample_count: int) -> str:
    return base64.b64encode(b"\x00\x00" * sample_count).decode("ascii")


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


class FakeWarmReusableQwenClient(FakeQwenClient):
    instances: list["FakeWarmReusableQwenClient"] = []

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        type(self).instances.append(self)
        self.closed = False

    async def commit_audio(self) -> None:
        await self.events.put(
            QwenEvent(kind="transcript_delta", payload={"type": "response.audio_transcript.delta"}, text="warm ")
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

    async def close(self) -> None:
        self.closed = True
        await self.events.put(None)


class FakeNeverFirstAudioQwenClient(FakeQwenClient):
    instances: list["FakeNeverFirstAudioQwenClient"] = []

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        type(self).instances.append(self)
        self.closed = False

    async def commit_audio(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def iter_events(self):
        try:
            while True:
                await asyncio.sleep(3600)
                if False:
                    yield None
        except asyncio.CancelledError:
            return


class FakeAudioThenHangQwenClient(FakeNeverFirstAudioQwenClient):
    async def commit_audio(self) -> None:
        await self.events.put(
            QwenEvent(
                kind="assistant_audio_delta",
                payload={"type": "response.audio.delta"},
                audio_base64=self.audio_chunks[-1],
                sample_rate=24000,
            )
        )

    async def iter_events(self):
        while True:
            event = await self.events.get()
            if event is not None:
                yield event


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


class FakeStreamingQwenChatClient(FakeQwenChatClient):
    last_instance = None

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        type(self).last_instance = self
        self.stream_requests: list[tuple[bytes, int, str]] = []

    async def stream_audio_response(
        self,
        *,
        audio_pcm16: bytes,
        sample_rate: int,
        speaker: str,
    ):
        self.stream_requests.append((audio_pcm16, sample_rate, speaker))
        yield QwenEvent(kind="assistant_text_delta", payload={"type": "response.text.delta"}, text="brief ")
        yield QwenEvent(
            kind="assistant_audio_delta",
            payload={"type": "response.audio.delta"},
            audio_base64=base64.b64encode(b"\x00\x00" * 960).decode("ascii"),
            sample_rate=24000,
        )
        yield QwenEvent(kind="response_done", payload={"type": "response.done"})


_OPEN_TEST_SESSIONS: list[RealtimeSession] = []


def _track_test_session(session: RealtimeSession) -> RealtimeSession:
    _OPEN_TEST_SESSIONS.append(session)
    return session


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_test_sessions():
    _OPEN_TEST_SESSIONS.clear()
    try:
        yield
    finally:
        for session in reversed(_OPEN_TEST_SESSIONS):
            await session.close()
        _OPEN_TEST_SESSIONS.clear()


def _session_settings(**overrides) -> Settings:
    return Settings(**overrides)


async def _start_audio_session(
    collector: EventCollector,
    *,
    settings: Settings | None = None,
) -> RealtimeSession:
    session = _track_test_session(RealtimeSession(
        settings=settings or _session_settings(QWEN_AUDIO_BACKEND="realtime"),
        emit_event=collector.emit,
    ))
    await session.start_session(
        {
            "speaker": "Ethan",
            "modalities": ["text", "audio"],
            "input_sample_rate": 16000,
            "output_audio": True,
        }
    )
    return session


async def _start_chat_stream_audio_session(collector: EventCollector) -> RealtimeSession:
    return await _start_audio_session(
        collector,
        settings=_session_settings(QWEN_AUDIO_BACKEND="chat_stream"),
    )


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
    session = RealtimeSession(
        settings=_session_settings(QWEN_AUDIO_BACKEND="realtime"),
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

    assert collector.events[0]["type"] == "error"
    assert collector.events[0]["code"] == "QWEN_UNAVAILABLE"


@pytest.mark.asyncio
async def test_session_streams_normalized_events(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeQwenClient)
    collector = EventCollector()
    session = await _start_audio_session(collector)

    assert collector.events[0]["type"] == "session.ready"
    assert collector.events[0]["backend"] == "qwen_realtime_audio"
    assert "degraded_reason" not in collector.events[0]
    assert collector.events[1]["type"] == "metrics.update"
    assert collector.events[1]["reconnects"] == {"startup": 1}
    assert collector.events[1]["last_reconnect_reason"] == "startup"
    assert collector.events[1]["timestamps"]["qwen_ws_connected"] is not None
    assert collector.events[1]["timestamps"]["qwen_session_ready"] is not None

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
    assert session.first_audio_timeout_task is None
    assert session.total_response_timeout_task is None


@pytest.mark.asyncio
async def test_session_buffers_assistant_output_until_commit(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeEarlyAssistantQwenClient)
    collector = EventCollector()
    session = await _start_audio_session(collector)
    session.begin_turn()
    session.mark_vad_speech_start()

    await session.append_audio_chunk(
        audio_base64=_valid_audio_base64(),
        sample_rate=16000,
    )
    await _drain_tasks()

    precommit_types = [event["type"] for event in collector.events]
    assert "transcript.delta" in precommit_types
    assert "assistant.audio.delta" not in precommit_types

    session.mark_vad_speech_end()
    await session.commit_audio()
    await session.mark_livekit_egress_started_with_queue_depth(queue_depth_ms=20.0)
    await _drain_tasks()

    seen_types = [event["type"] for event in collector.events]
    assert "assistant.audio.delta" in seen_types
    metrics_events = [event for event in collector.events if event["type"] == "metrics.update"]
    assert metrics_events
    final_metrics = metrics_events[-1]["metrics"]
    assert final_metrics["queue_depth_at_first_frame_ms"] == 20.0
    assert final_metrics["commit_to_qwen_first_audio_ms"] <= 1
    assert final_metrics["commit_to_first_transcript_ms"] <= 1
    assert final_metrics["commit_to_first_audio_delta_ms"] <= 1


@pytest.mark.asyncio
async def test_session_accepts_internal_pcm16_audio_bytes(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeQwenClient)
    collector = EventCollector()
    session = await _start_audio_session(collector)

    raw_audio = b"\x01\x00" * 1280
    expected_audio_base64 = base64.b64encode(raw_audio).decode("ascii")

    await session.append_audio_bytes(
        audio_bytes=raw_audio,
        sample_rate=16000,
    )

    client = FakeQwenClient.last_instance
    assert client is not None
    assert client.audio_chunks == [expected_audio_base64]

    await session.commit_audio()
    await _drain_tasks()

    assert any(event["type"] == "assistant.audio.delta" for event in collector.events)


@pytest.mark.asyncio
async def test_session_caps_buffered_precommit_assistant_events(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "MAX_BUFFERED_ASSISTANT_EVENTS", 3)
    collector = EventCollector()
    session = RealtimeSession(
        settings=_session_settings(QWEN_AUDIO_BACKEND="realtime"),
        emit_event=collector.emit,
    )
    session.session_config = RealtimeSessionConfig(
        speaker="Ethan",
        modalities=["text", "audio"],
        input_sample_rate=16000,
        output_audio=True,
    )
    session.assistant_output_gate_open = False

    for index in range(4):
        await session._handle_qwen_event(
            QwenEvent(
                kind="assistant_text_delta",
                payload={"type": "response.text.delta"},
                text=str(index),
            ),
            output_version=session.output_version,
        )
    await session._handle_qwen_event(
        QwenEvent(
            kind="response_done",
            payload={"type": "response.done"},
        ),
        output_version=session.output_version,
    )

    assert len(session.buffered_assistant_events) == 3
    assert [event.kind for _, event in session.buffered_assistant_events] == [
        "assistant_text_delta",
        "assistant_text_delta",
        "response_done",
    ]

    await session._flush_buffered_assistant_events()

    emitted_text_events = [
        event for event in collector.events if event["type"] == "assistant.text.delta"
    ]
    assert [event["text"] for event in emitted_text_events] == ["2", "3"]
    assert any(event["type"] == "assistant.done" for event in collector.events)


@pytest.mark.asyncio
async def test_session_emits_metrics_only_on_first_egress_and_turn_close() -> None:
    collector = EventCollector()
    session = RealtimeSession(
        settings=_session_settings(QWEN_AUDIO_BACKEND="realtime"),
        emit_event=collector.emit,
    )
    session.session_config = RealtimeSessionConfig(
        speaker="Ethan",
        modalities=["text", "audio"],
        input_sample_rate=16000,
        output_audio=True,
    )
    session.last_metrics_snapshot = {}
    session.begin_turn()
    session.mark_vad_speech_start()
    session.metrics.mark_once("first_user_audio_uploaded")
    session.mark_vad_speech_end()
    session.metrics.mark_once("commit_sent")

    await session._handle_qwen_event(
        QwenEvent(
            kind="transcript_delta",
            payload={"type": "response.audio_transcript.delta"},
            text="hello ",
        ),
        output_version=session.output_version,
    )
    await session._handle_qwen_event(
        QwenEvent(
            kind="assistant_audio_delta",
            payload={"type": "response.audio.delta"},
            audio_base64=_valid_audio_base64(),
            sample_rate=24000,
        ),
        output_version=session.output_version,
    )

    assert [event["type"] for event in collector.events] == [
        "transcript.delta",
        "assistant.audio.delta",
    ]

    await session.mark_livekit_egress_started_with_queue_depth(queue_depth_ms=25.0)
    assert collector.events[-1]["type"] == "metrics.update"
    assert collector.events[-1]["metrics"]["queue_depth_at_first_frame_ms"] == 25.0
    assert collector.events[-1]["metrics"]["vad_end_to_commit_ms"] == 0.0

    await session._handle_qwen_event(
        QwenEvent(
            kind="response_done",
            payload={"type": "response.done"},
        ),
        output_version=session.output_version,
    )

    assert collector.events[-2]["type"] == "assistant.done"
    assert collector.events[-1]["type"] == "metrics.update"
    assert collector.events[-1]["metrics"]["speech_end_to_first_assistant_egress_ms"] is not None
    assert collector.events[-1]["timestamps"]["assistant_done"] is not None
    assert collector.events[-1]["timestamps"]["turn_closed"] is not None


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
    session = _track_test_session(RealtimeSession(settings=Settings(), emit_event=collector.emit))

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

    assert collector.events[0]["type"] == "session.ready"
    assert collector.events[0]["backend"] == "text_only"
    assert collector.events[0]["degraded_reason"] == "non_realtime_audio_backend"
    seen_types = [event["type"] for event in collector.events]
    assert "assistant.audio.delta" not in seen_types
    assert "assistant.text.delta" in seen_types
    assert collector.events[-1]["type"] == "metrics.update"


@pytest.mark.asyncio
async def test_session_audio_mode_uses_streaming_chat_completions(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenChatClient", FakeStreamingQwenChatClient)
    collector = EventCollector()
    session = await _start_chat_stream_audio_session(collector)

    await session.append_audio_chunk(
        audio_base64=_valid_audio_base64(),
        sample_rate=16000,
    )
    await session.commit_audio()
    await _wait_for_event(collector, "assistant.done")

    assert collector.events[0]["type"] == "session.ready"
    assert collector.events[0]["backend"] == "qwen_chat_stream_audio"
    assert collector.events[0]["degraded_reason"] == "non_realtime_audio_backend"
    client = FakeStreamingQwenChatClient.last_instance
    assert client is not None
    assert len(client.stream_requests) == 1
    request_audio, request_sample_rate, request_speaker = client.stream_requests[0]
    assert request_audio
    assert request_sample_rate == 16000
    assert request_speaker == "Ethan"

    seen_types = [event["type"] for event in collector.events]
    assert "transcript.delta" not in seen_types
    assert "assistant.text.delta" in seen_types
    assert "assistant.audio.delta" in seen_types


@pytest.mark.asyncio
async def test_session_cancel_restarts_upstream_qwen_session(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeRestartableQwenClient)
    FakeRestartableQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    try:
        await session.append_audio_chunk(
            audio_base64=_valid_audio_base64(),
            sample_rate=16000,
        )
        await session.commit_audio()
        await _drain_tasks()

        output_version_before_interrupt = session.output_version
        await session.cancel_response()
        await session.append_audio_chunk(
            audio_base64=_valid_audio_base64(),
            sample_rate=16000,
        )
        await session.commit_audio()
        await _drain_tasks()

        assert len(FakeRestartableQwenClient.instances) == 2
        assert session.output_version == output_version_before_interrupt + 1
        assert FakeRestartableQwenClient.instances[0].cancelled is True
        assert FakeRestartableQwenClient.instances[0].closed is True
        metrics_events = [event for event in collector.events if event["type"] == "metrics.update"]
        assert metrics_events[-1]["reconnects"]["interrupt"] == 1
        assert metrics_events[-1]["last_reconnect_reason"] == "interrupt"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_reuses_warm_qwen_session_across_completed_turns(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeWarmReusableQwenClient)
    FakeWarmReusableQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    try:
        await session.append_audio_chunk(
            audio_base64=_valid_audio_base64(),
            sample_rate=16000,
        )
        await session.commit_audio()
        await _wait_for_event(collector, "assistant.done")
        first_done_count = sum(1 for event in collector.events if event["type"] == "assistant.done")

        await session.append_audio_chunk(
            audio_base64=_valid_audio_base64(),
            sample_rate=16000,
        )
        await session.commit_audio()

        for _ in range(20):
            if sum(1 for event in collector.events if event["type"] == "assistant.done") >= first_done_count + 1:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("Timed out waiting for the second assistant.done event.")

        assert len(FakeWarmReusableQwenClient.instances) == 1
        assert FakeWarmReusableQwenClient.instances[0].closed is False
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_times_out_when_first_audio_never_arrives(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeNeverFirstAudioQwenClient)
    FakeNeverFirstAudioQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(
        collector,
        settings=_session_settings(
            QWEN_AUDIO_BACKEND="realtime",
            QWEN_FIRST_AUDIO_TIMEOUT_SECONDS=0.01,
            QWEN_TOTAL_RESPONSE_TIMEOUT_SECONDS=0.05,
        ),
    )

    try:
        await session.append_audio_chunk(
            audio_base64=_valid_audio_base64(),
            sample_rate=16000,
        )
        await session.commit_audio()
        await asyncio.sleep(0.03)
        await _wait_for_event(collector, "error")

        error_events = [event for event in collector.events if event["type"] == "error"]
        assert error_events[-1]["code"] == "QWEN_FIRST_AUDIO_TIMEOUT"
        assert len(FakeNeverFirstAudioQwenClient.instances) >= 2
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_times_out_when_response_never_finishes(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeAudioThenHangQwenClient)
    FakeAudioThenHangQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(
        collector,
        settings=_session_settings(
            QWEN_AUDIO_BACKEND="realtime",
            QWEN_FIRST_AUDIO_TIMEOUT_SECONDS=0.01,
            QWEN_TOTAL_RESPONSE_TIMEOUT_SECONDS=0.03,
        ),
    )

    try:
        await session.append_audio_chunk(
            audio_base64=_valid_audio_base64(),
            sample_rate=16000,
        )
        await session.commit_audio()
        await _wait_for_event(collector, "assistant.audio.delta")
        await asyncio.sleep(0.04)
        await _wait_for_event(collector, "error")

        error_events = [event for event in collector.events if event["type"] == "error"]
        assert error_events[-1]["code"] == "QWEN_RESPONSE_TIMEOUT"
        assert len(FakeAudioThenHangQwenClient.instances) >= 2
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_rejects_turn_audio_that_exceeds_configured_turn_limit(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeQwenClient)
    collector = EventCollector()
    session = await _start_audio_session(
        collector,
        settings=_session_settings(
            QWEN_AUDIO_BACKEND="realtime",
            LIVEKIT_MAX_TURN_MS=1000,
        ),
    )

    try:
        chunk = _audio_base64_for_samples(3200)
        for _ in range(5):
            await session.append_audio_chunk(
                audio_base64=chunk,
                sample_rate=16000,
            )

        client = FakeQwenClient.last_instance
        assert client is not None
        assert len(client.audio_chunks) == 5

        await session.append_audio_chunk(
            audio_base64=chunk,
            sample_rate=16000,
        )

        error_events = [event for event in collector.events if event["type"] == "error"]
        assert error_events[-1]["code"] == "TURN_AUDIO_LIMIT_EXCEEDED"
        assert len(client.audio_chunks) == 5
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_drops_stale_audio_delta_by_output_version() -> None:
    collector = EventCollector()
    session = RealtimeSession(
        settings=_session_settings(QWEN_AUDIO_BACKEND="realtime"),
        emit_event=collector.emit,
    )
    session.session_config = RealtimeSessionConfig(
        speaker="Ethan",
        modalities=["text", "audio"],
        input_sample_rate=16000,
        output_audio=True,
    )
    stale_output_version = session.output_version
    session._bump_output_version("test_interrupt")

    await session._handle_qwen_event(
        QwenEvent(
            kind="assistant_audio_delta",
            payload={"type": "response.audio.delta"},
            audio_base64=_valid_audio_base64(),
            sample_rate=24000,
        ),
        output_version=stale_output_version,
    )

    seen_types = [event["type"] for event in collector.events]
    assert "assistant.audio.delta" not in seen_types
    assert session.assistant_audio_chunk_count == 0


@pytest.mark.asyncio
async def test_session_new_turn_restarts_stale_upstream_qwen_session(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeStaleResponseQwenClient)
    FakeStaleResponseQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    try:
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
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_recovers_turn_after_qwen_append_restart(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeAppendRecoveringQwenClient)
    FakeAppendRecoveringQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    try:
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
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_recovers_turn_after_qwen_commit_restart(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeCommitRecoveringQwenClient)
    FakeCommitRecoveringQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    try:
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
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_retries_buffered_commit_after_qwen_connect_refused(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "QwenRealtimeClient", FakeReconnectRefusedOnBufferedCommitQwenClient)
    FakeReconnectRefusedOnBufferedCommitQwenClient.instances = []
    collector = EventCollector()
    session = await _start_audio_session(collector)

    try:
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
    finally:
        await session.close()
