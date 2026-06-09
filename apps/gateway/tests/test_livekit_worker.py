import asyncio
import base64
import json
import logging
from array import array

import pytest

import src.livekit.worker as worker_module
import src.realtime.session as session_module
from src.config import Settings
from src.realtime.qwen_client import QwenEvent


class FakeRoom:
    def __init__(self) -> None:
        self.local_participant = FakeLocalParticipant()

    def on(self, _event_name: str):
        def decorator(callback):
            return callback

        return decorator


class FakeLocalParticipant:
    def __init__(self) -> None:
        self.published_payloads: list[dict] = []
        self.publish_calls: list[dict] = []

    async def publish_data(
        self,
        payload: str,
        *,
        reliable: bool,
        destination_identities: list[str],
        topic: str,
    ) -> None:
        assert destination_identities
        assert topic
        decoded_payload = json.loads(payload)
        self.published_payloads.append(decoded_payload)
        self.publish_calls.append(
            {
                "payload": decoded_payload,
                "reliable": reliable,
                "destination_identities": destination_identities,
                "topic": topic,
            }
        )


class FakeAudioSource:
    def __init__(
        self,
        sample_rate: int,
        num_channels: int,
        queue_size_ms: int = 1000,
        loop=None,
    ) -> None:
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.queue_size_ms = queue_size_ms
        self.loop = loop


class FakeAudioPublisher:
    def __init__(
        self,
        *,
        audio_source,
        output_sample_rate: int,
        output_frame_ms: int,
        frame_sink=None,
    ) -> None:
        self.audio_source = audio_source
        self.output_sample_rate = output_sample_rate
        self.output_frame_ms = output_frame_ms
        self.frame_sink = frame_sink

    def set_log_context(self, **_kwargs) -> None:
        return None


class RecordingAudioPublisher:
    def __init__(self) -> None:
        self.enqueued_audio: list[tuple[str, int]] = []
        self.finalize_calls = 0
        self.clear_calls = 0
        self.queued_duration = 0.0

    def set_log_context(self, **_kwargs) -> None:
        return None

    async def enqueue_base64(self, audio_base64: str, *, input_sample_rate: int) -> None:
        self.enqueued_audio.append((audio_base64, input_sample_rate))
        self.queued_duration += 0.02

    async def finalize_turn(self) -> None:
        self.finalize_calls += 1

    async def clear(self) -> None:
        self.clear_calls += 1
        self.queued_duration = 0.0

    def queued_duration_seconds(self) -> float:
        return self.queued_duration


class FakePublishingAudioSource:
    def __init__(self, *, queue_size_ms: int = 150) -> None:
        self.frames = []
        self.queue_size_ms = queue_size_ms
        self._queued_duration = 0.0

    async def capture_frame(self, frame) -> None:
        self.frames.append(frame)

    def clear_queue(self) -> None:
        self.frames.clear()

    async def wait_for_playout(self) -> None:
        return None

    @property
    def queued_duration(self) -> float:
        return self._queued_duration


class FakeRealtimeSession:
    def __init__(
        self,
        *,
        settings: Settings,
        emit_event,
        participant_identity: str | None = None,
        room: str | None = None,
    ) -> None:
        del settings
        del participant_identity
        del room
        self.emit_event = emit_event
        self.session_id = "session-test"
        self.livekit_egress_marks = 0
        self.last_queue_depth_ms: float | None = None
        self.turn_begin_calls = 0
        self.turn_ids: list[str | None] = []
        self.vad_speech_start_marks = 0
        self.vad_speech_end_marks = 0
        self.browser_first_audio_played_ms: float | None = None
        self.flush_metrics_update_calls = 0
        self.append_calls: list[dict] = []
        self.commit_calls = 0
        self.cancel_calls = 0
        self.start_payloads: list[dict] = []
        self.output_version = 1
        self.active_response = False

    async def start_session(self, payload: dict) -> None:
        self.start_payloads.append(payload)
        return None

    async def append_audio_chunk(self, **kwargs) -> None:
        self.append_calls.append(kwargs)
        return None

    async def append_audio_bytes(self, **kwargs) -> None:
        self.append_calls.append(kwargs)
        return None

    async def commit_audio(self) -> None:
        self.commit_calls += 1
        audio_base64 = base64.b64encode(b"\x00\x00" * 960).decode("ascii")
        await self.emit_event(
            {
                "type": "assistant.audio.delta",
                "audio_base64": audio_base64,
                "sample_rate": 24000,
                "channels": 1,
                "format": "pcm16",
                "output_version": self.output_version,
            }
        )
        await self.emit_event({"type": "assistant.done", "output_version": self.output_version})

    async def cancel_response(self) -> None:
        self.cancel_calls += 1
        return None

    async def close(self) -> None:
        return None

    def begin_turn(self, *, turn_id: str | None = None) -> None:
        self.turn_begin_calls += 1
        self.turn_ids.append(turn_id)

    def mark_vad_speech_start(self) -> None:
        self.vad_speech_start_marks += 1

    def mark_vad_speech_end(self) -> None:
        self.vad_speech_end_marks += 1

    def mark_livekit_egress_started(self) -> None:
        self.livekit_egress_marks += 1

    async def mark_livekit_egress_started_with_queue_depth(
        self,
        *,
        queue_depth_ms: float | None,
    ) -> None:
        self.livekit_egress_marks += 1
        self.last_queue_depth_ms = queue_depth_ms

    def mark_browser_first_audio_played(self, *, latency_ms: float | None) -> None:
        self.browser_first_audio_played_ms = latency_ms

    async def flush_metrics_update(self) -> None:
        self.flush_metrics_update_calls += 1

    def has_active_response(self) -> bool:
        return self.active_response


class FakeInterruptingRealtimeSession(FakeRealtimeSession):
    async def cancel_response(self) -> None:
        self.cancel_calls += 1
        self.output_version += 1
        self.active_response = False


class StubRealtimeQwenClient:
    def __init__(self, *, output_sample_rate: int) -> None:
        self.output_sample_rate = output_sample_rate
        self.events: asyncio.Queue[QwenEvent | None] = asyncio.Queue()
        self.closed = False

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
        del speaker, modalities, input_sample_rate, output_audio
        return None

    async def append_audio(self, pcm16_base64: str) -> None:
        del pcm16_base64
        return None

    async def commit_audio(self) -> None:
        async def emit_response() -> None:
            await asyncio.sleep(0.005)
            if self.closed:
                return
            audio_base64 = base64.b64encode(b"\xb0\x04" * 960).decode("ascii")
            await self.events.put(
                QwenEvent(
                    kind="assistant_audio_delta",
                    payload={"type": "response.audio.delta"},
                    audio_base64=audio_base64,
                    sample_rate=self.output_sample_rate,
                )
            )
            await asyncio.sleep(0.005)
            if self.closed:
                return
            await self.events.put(
                QwenEvent(
                    kind="response_done",
                    payload={"type": "response.done"},
                )
            )

        asyncio.create_task(emit_response())

    async def cancel_response(self) -> None:
        self.closed = True
        await self.events.put(None)

    async def close(self) -> None:
        self.closed = True
        await self.events.put(None)

    async def iter_events(self):
        while True:
            event = await self.events.get()
            if event is None:
                return
            yield event


class FakeVADStream:
    def __init__(self) -> None:
        self.events: asyncio.Queue[worker_module.VADEvent | None] = asyncio.Queue()
        self.pushed_frames = []

    def push_frame(self, frame) -> None:
        self.pushed_frames.append(frame)

    async def aclose(self) -> None:
        await self.events.put(None)

    def emit(self, event: worker_module.VADEvent) -> None:
        self.events.put_nowait(event)

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration
        return event


class FakeVAD:
    def __init__(self, stream: FakeVADStream) -> None:
        self._stream = stream

    def stream(self) -> FakeVADStream:
        return self._stream


class FakeAudioStream:
    last_kwargs: dict | None = None

    def __init__(self, track, *, sample_rate: int, num_channels: int, frame_size_ms: int) -> None:
        del track
        type(self).last_kwargs = {
            "sample_rate": sample_rate,
            "num_channels": num_channels,
            "frame_size_ms": frame_size_ms,
        }
        self._remaining_frames = 4

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._remaining_frames <= 0:
            raise StopAsyncIteration
        self._remaining_frames -= 1
        return type("FrameEvent", (), {"frame": _pcm_frame(sample_rate=16000, samples=[1000] * 320)})()

    async def aclose(self) -> None:
        return None


def _pcm_frame(*, sample_rate: int, samples: list[int]) -> worker_module.rtc.AudioFrame:
    pcm16 = array("h", samples)
    return worker_module.rtc.AudioFrame(
        data=pcm16.tobytes(),
        sample_rate=sample_rate,
        num_channels=1,
        samples_per_channel=len(samples),
    )


class DummyClosableSession:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def test_livekit_worker_uses_configured_audio_queue(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.rtc, "Room", FakeRoom)
    monkeypatch.setattr(worker_module.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(worker_module, "AssistantAudioPublisher", FakeAudioPublisher)
    monkeypatch.setattr(worker_module, "build_input_vad", lambda _settings: None)

    settings = Settings(
        LIVEKIT_OUTPUT_SAMPLE_RATE=48000,
        LIVEKIT_OUTPUT_FRAME_MS=10,
        LIVEKIT_OUTPUT_QUEUE_MS=120,
        LIVEKIT_PREROLL_FRAMES=1,
        QWEN_AUDIO_INPUT_SAMPLE_RATE=16000,
        QWEN_AUDIO_OUTPUT_SAMPLE_RATE=24000,
    )

    worker = worker_module.LiveKitWorker(settings)

    assert worker.audio_source.sample_rate == 48000
    assert worker.audio_source.num_channels == 1
    assert worker.audio_source.queue_size_ms == 120
    assert worker.audio_publisher.output_sample_rate == 48000
    assert worker.audio_publisher.output_frame_ms == 10


@pytest.mark.asyncio
async def test_livekit_worker_request_drain_sets_stop_event_without_sessions(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.rtc, "Room", FakeRoom)
    monkeypatch.setattr(worker_module.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(worker_module, "AssistantAudioPublisher", FakeAudioPublisher)
    monkeypatch.setattr(worker_module, "build_input_vad", lambda _settings: None)

    worker = worker_module.LiveKitWorker(Settings())

    await worker.request_drain(reason="test")

    assert worker.draining is True
    assert worker.stop_event.is_set() is True


@pytest.mark.asyncio
async def test_livekit_worker_force_drains_active_sessions_after_timeout(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.rtc, "Room", FakeRoom)
    monkeypatch.setattr(worker_module.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(worker_module, "AssistantAudioPublisher", FakeAudioPublisher)
    monkeypatch.setattr(worker_module, "build_input_vad", lambda _settings: None)

    worker = worker_module.LiveKitWorker(Settings(LIVEKIT_DRAIN_TIMEOUT_SECONDS=0.01))
    dummy_session = DummyClosableSession()
    worker.sessions["browser-a"] = dummy_session
    worker.active_participant_identity = "browser-a"

    await worker.request_drain(reason="test")
    await asyncio.sleep(0.02)

    assert dummy_session.close_calls == 1
    assert worker.stop_event.is_set() is True
    assert worker.active_participant_identity is None


@pytest.mark.asyncio
async def test_livekit_worker_rejects_new_subscribed_track_while_draining(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.rtc, "Room", FakeRoom)
    monkeypatch.setattr(worker_module.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(worker_module, "AssistantAudioPublisher", FakeAudioPublisher)
    monkeypatch.setattr(worker_module, "build_input_vad", lambda _settings: None)

    worker = worker_module.LiveKitWorker(Settings())
    worker.draining = True

    track = type("AudioTrack", (), {"kind": worker_module.rtc.TrackKind.KIND_AUDIO})()
    participant = type("Participant", (), {"identity": "browser-drain"})()

    await worker._handle_track_subscribed(track, participant)

    assert worker.room.local_participant.published_payloads[-1]["type"] == "room.busy"
    assert "draining" in worker.room.local_participant.published_payloads[-1]["message"]


@pytest.mark.asyncio
async def test_livekit_worker_rejects_second_participant_audio_subscription(monkeypatch) -> None:
    class FakeRoomService:
        def __init__(self) -> None:
            self.removed_participants: list[object] = []

        async def remove_participant(self, participant_identity) -> None:
            self.removed_participants.append(participant_identity)

    class FakeLiveKitAPI:
        instances: list["FakeLiveKitAPI"] = []

        def __init__(self, *_args) -> None:
            self.room = FakeRoomService()
            self.closed = False
            type(self).instances.append(self)

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(worker_module.rtc, "Room", FakeRoom)
    monkeypatch.setattr(worker_module.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(worker_module, "AssistantAudioPublisher", FakeAudioPublisher)
    monkeypatch.setattr(worker_module, "build_input_vad", lambda _settings: None)
    monkeypatch.setattr(worker_module.livekit_api, "LiveKitAPI", FakeLiveKitAPI)

    settings = Settings(
        LIVEKIT_URL="ws://185.62.58.164:7880",
        LIVEKIT_API_KEY="key",
        LIVEKIT_API_SECRET="secret",
        LIVEKIT_ROOM="omni-room",
    )
    worker = worker_module.LiveKitWorker(settings)
    worker.active_participant_identity = "browser-a"

    track = type("AudioTrack", (), {"kind": worker_module.rtc.TrackKind.KIND_AUDIO})()
    participant = type("Participant", (), {"identity": "browser-b"})()

    await worker._handle_track_subscribed(track, participant)

    assert "browser-b" not in worker.sessions
    assert worker.room.local_participant.published_payloads[-1] == {
        "type": "room.busy",
        "code": "ROOM_BUSY",
        "message": "This LiveKit room currently supports one active browser session.",
        "room": "omni-room",
        "participant_id": "browser-b",
        "participant_identity": "browser-b",
    }
    assert len(FakeLiveKitAPI.instances) == 1
    removed = FakeLiveKitAPI.instances[0].room.removed_participants
    assert len(removed) == 1
    assert removed[0].room == "omni-room"
    assert removed[0].identity == "browser-b"
    assert FakeLiveKitAPI.instances[0].closed is True


def test_livekit_output_defaults_match_supported_transport() -> None:
    settings = Settings()

    assert settings.livekit_input_sample_rate == 48000
    assert settings.livekit_output_sample_rate == 48000
    assert settings.qwen_input_sample_rate == 16000
    assert settings.qwen_output_sample_rate == 24000
    assert settings.livekit_output_frame_ms == 10
    assert 150 <= settings.livekit_output_queue_ms <= 200
    assert settings.livekit_preroll_frames == 1


def test_livekit_worker_rejects_24k_transport_publish_rate() -> None:
    with pytest.raises(ValueError, match="LIVEKIT_OUTPUT_SAMPLE_RATE"):
        Settings(
            LIVEKIT_OUTPUT_SAMPLE_RATE=24000,
            QWEN_AUDIO_OUTPUT_SAMPLE_RATE=24000,
        )


def test_assistant_track_publish_options_disable_dtx_and_enable_red() -> None:
    options = worker_module.build_assistant_track_publish_options()

    assert options.source == worker_module.rtc.TrackSource.SOURCE_MICROPHONE
    assert options.dtx is False
    assert options.red is True
    assert options.audio_encoding.max_bitrate == worker_module.ASSISTANT_TRACK_MAX_BITRATE


@pytest.mark.asyncio
async def test_livekit_commit_keeps_pre_audio_control_events_until_first_audio(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    room = FakeRoom()
    audio_publisher = RecordingAudioPublisher()
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=room,
        audio_publisher=audio_publisher,
        metric_rollups=worker_module.MetricRollupWindow(),
    )

    await session.handle_control_message({"type": "session.start"})

    assert session.session.start_payloads[-1]["input_sample_rate"] == 16000

    await session.handle_control_message({"type": "client.speech.start"})
    await session._emit_runtime_event({"type": "transcript.delta", "text": "hello"})
    await session._emit_runtime_event(
        {
            "type": "metrics.update",
            "metrics": {"commit_to_first_audio_delta_ms": None},
            "timestamps": {"t_audio_commit_sent": None},
        }
    )

    assert room.local_participant.published_payloads == []

    await session.handle_control_message({"type": "client.speech.commit"})

    assert len(audio_publisher.enqueued_audio) == 1
    assert audio_publisher.enqueued_audio[0][1] == 24000
    assert audio_publisher.finalize_calls == 1
    published_types = [payload["type"] for payload in room.local_participant.published_payloads]
    assert published_types == [
        "turn.committed",
        "assistant.audio_started",
        "transcript.delta",
        "metrics.update",
        "assistant.done",
    ]
    assert room.local_participant.published_payloads[0]["input_audio_ms"] == 0.0
    assert all(payload["output_version"] == 1 for payload in room.local_participant.published_payloads)
    assert all(payload["participant_id"] == "browser-test" for payload in room.local_participant.published_payloads)
    assert all(payload["session_id"] == "session-test" for payload in room.local_participant.published_payloads)
    assert all(payload["turn_id"] == "1" for payload in room.local_participant.published_payloads)
    assert room.local_participant.publish_calls[0]["reliable"] is True
    assert room.local_participant.publish_calls[1]["reliable"] is True
    assert room.local_participant.publish_calls[2]["reliable"] is True
    assert room.local_participant.publish_calls[3]["reliable"] is False
    assert room.local_participant.publish_calls[4]["reliable"] is True
    metrics_payload = room.local_participant.published_payloads[3]
    assert metrics_payload["metrics"]["assistant_queue_depth_ms"] == 20.0
    assert "interrupt_clear_ms" not in metrics_payload["metrics"]
    assert metrics_payload["turn_id"] == "1"
    assert metrics_payload["rollups"]["assistant_queue_depth_last_ms"] == 20.0
    assert metrics_payload["rollups"]["assistant_queue_depth_p95_ms"] == 20.0
    assert room.local_participant.published_payloads[-1]["turn_id"] == "1"


@pytest.mark.asyncio
async def test_livekit_rejects_manual_turn_control_when_server_vad_is_enabled(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    room = FakeRoom()
    vad_stream = FakeVADStream()
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=room,
        audio_publisher=RecordingAudioPublisher(),
        input_vad=FakeVAD(vad_stream),
    )

    try:
        await session.handle_control_message({"type": "session.start"})
        await session.handle_control_message({"type": "client.speech.start"})
        await session.handle_control_message({"type": "client.speech.commit"})

        assert session.turn_active is False
        assert session.session.append_calls == []
        assert session.session.commit_calls == 0
        assert room.local_participant.published_payloads == [
            {
                "type": "error",
                "code": "TURN_CONTROL_DISABLED",
                "message": (
                    "Server-side VAD owns speech start and commit while "
                    "LIVEKIT_INPUT_VAD_ENABLED is true."
                ),
                "participant_id": "browser-test",
                "participant_identity": "browser-test",
                "room": "omni-room",
                "session_id": "session-test",
                "output_version": 1,
                "input_epoch": 0,
                "interrupt_epoch": 0,
            },
            {
                "type": "error",
                "code": "TURN_CONTROL_DISABLED",
                "message": (
                    "Server-side VAD owns speech start and commit while "
                    "LIVEKIT_INPUT_VAD_ENABLED is true."
                ),
                "participant_id": "browser-test",
                "participant_identity": "browser-test",
                "room": "omni-room",
                "session_id": "session-test",
                "output_version": 1,
                "input_epoch": 0,
                "interrupt_epoch": 0,
            },
        ]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_livekit_audio_stream_resamples_transport_input_to_qwen_rate(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)
    monkeypatch.setattr(worker_module.rtc, "AudioStream", FakeAudioStream)
    FakeAudioStream.last_kwargs = None

    session = worker_module.ParticipantBridgeSession(
        settings=Settings(LIVEKIT_INPUT_SAMPLE_RATE=48000, QWEN_AUDIO_INPUT_SAMPLE_RATE=16000),
        participant_identity="browser-test",
        room=FakeRoom(),
        audio_publisher=RecordingAudioPublisher(),
    )

    await session.handle_control_message({"type": "session.start"})
    await session.handle_control_message({"type": "client.speech.start"})
    await session._consume_audio_stream(object())

    assert FakeAudioStream.last_kwargs == {
        "sample_rate": 16000,
        "num_channels": 1,
        "frame_size_ms": 20,
    }
    assert len(session.session.append_calls) == 1
    assert session.session.append_calls[0]["sample_rate"] == 16000
    assert session.session.append_calls[0]["audio_bytes"]
    assert "audio_base64" not in session.session.append_calls[0]


@pytest.mark.asyncio
async def test_livekit_commit_forwards_audio_metadata_only_in_debug_mode(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    room = FakeRoom()
    audio_publisher = RecordingAudioPublisher()
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=room,
        audio_publisher=audio_publisher,
    )

    await session.handle_control_message({"type": "session.start", "debug_audio_metadata": True})
    await session.handle_control_message({"type": "client.speech.start"})
    await session._emit_runtime_event({"type": "transcript.delta", "text": "hello"})

    await session.handle_control_message({"type": "client.speech.commit"})

    assert len(audio_publisher.enqueued_audio) == 1
    published_types = [payload["type"] for payload in room.local_participant.published_payloads]
    assert published_types == [
        "turn.committed",
        "assistant.audio_started",
        "transcript.delta",
        "assistant.audio.metadata",
        "assistant.done",
    ]
    assert room.local_participant.published_payloads[0]["input_audio_ms"] == 0.0
    assert room.local_participant.published_payloads[0]["participant_id"] == "browser-test"
    assert room.local_participant.published_payloads[0]["session_id"] == "session-test"
    assert room.local_participant.published_payloads[1]["output_version"] == 1
    assert room.local_participant.publish_calls[3]["reliable"] is False
    metadata_payload = room.local_participant.published_payloads[3]
    assert metadata_payload["sample_rate"] == 24000
    assert metadata_payload["output_version"] == 1
    assert "audio_base64" not in metadata_payload


@pytest.mark.asyncio
async def test_livekit_drops_stale_audio_before_egress(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    room = FakeRoom()
    audio_publisher = RecordingAudioPublisher()
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=room,
        audio_publisher=audio_publisher,
    )

    await session.handle_control_message({"type": "session.start"})
    session.session.output_version = 2
    await session._emit_runtime_event(
        {
            "type": "assistant.audio.delta",
            "audio_base64": base64.b64encode(b"\x00\x00" * 960).decode("ascii"),
            "sample_rate": 24000,
            "channels": 1,
            "format": "pcm16",
            "output_version": 1,
        }
    )

    assert audio_publisher.enqueued_audio == []
    assert room.local_participant.published_payloads == []


@pytest.mark.asyncio
async def test_livekit_speech_start_hard_interrupts_queued_assistant_audio(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    room = FakeRoom()
    audio_publisher = RecordingAudioPublisher()
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=room,
        audio_publisher=audio_publisher,
    )

    await session.handle_control_message({"type": "session.start"})
    await session._emit_runtime_event(
        {
            "type": "assistant.audio.delta",
            "audio_base64": base64.b64encode(b"\x00\x00" * 960).decode("ascii"),
            "sample_rate": 24000,
            "channels": 1,
            "format": "pcm16",
            "output_version": 1,
        }
    )
    await session._emit_runtime_event({"type": "assistant.done", "output_version": 1})

    assert session.session.has_active_response() is False
    assert audio_publisher.queued_duration_seconds() > 0

    await session.handle_control_message({"type": "client.speech.start"})

    assert audio_publisher.clear_calls == 1
    assert audio_publisher.queued_duration_seconds() == 0
    assert session.session.cancel_calls == 1
    assert session.turn_active is True
    assert room.local_participant.published_payloads[-1]["type"] == "assistant.interrupted"
    assert room.local_participant.published_payloads[-1]["output_version"] == 1


@pytest.mark.asyncio
async def test_livekit_interrupt_drops_late_stale_audio_from_old_output_version(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeInterruptingRealtimeSession)

    room = FakeRoom()
    audio_publisher = RecordingAudioPublisher()
    worker_state_store = worker_module.LiveKitWorkerStateStore(
        path=str(tmp_path / "livekit-worker-state.json")
    )
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=room,
        audio_publisher=audio_publisher,
        metric_rollups=worker_module.MetricRollupWindow(),
        worker_state_store=worker_state_store,
    )

    await session.handle_control_message({"type": "session.start"})
    session.session.active_response = True
    await session._emit_runtime_event(
        {
            "type": "assistant.audio.delta",
            "audio_base64": base64.b64encode(b"\x00\x00" * 960).decode("ascii"),
            "sample_rate": 24000,
            "channels": 1,
            "format": "pcm16",
            "output_version": 1,
        }
    )
    prior_enqueued_count = len(audio_publisher.enqueued_audio)

    await session.handle_control_message({"type": "client.interrupt"})
    await session._emit_runtime_event(
        {
            "type": "assistant.audio.delta",
            "audio_base64": base64.b64encode(b"\x00\x00" * 960).decode("ascii"),
            "sample_rate": 24000,
            "channels": 1,
            "format": "pcm16",
            "output_version": 1,
        }
    )

    assert session.session.output_version == 2
    assert session.session.cancel_calls == 1
    assert len(audio_publisher.enqueued_audio) == prior_enqueued_count
    assert room.local_participant.published_payloads[-1]["type"] == "assistant.interrupted"
    assert room.local_participant.published_payloads[-1]["output_version"] == 2
    assert room.local_participant.published_payloads[-1]["participant_id"] == "browser-test"
    assert room.local_participant.published_payloads[-1]["session_id"] == "session-test"
    assert worker_state_store.snapshot["interruption_count"] == 1
    assert worker_state_store.snapshot["stale_output_drop_count"] == 1


@pytest.mark.asyncio
async def test_livekit_metrics_include_interrupt_clear_time_after_interrupt(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeInterruptingRealtimeSession)

    room = FakeRoom()
    worker_state_store = worker_module.LiveKitWorkerStateStore(
        path=str(tmp_path / "livekit-worker-state.json")
    )
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=room,
        audio_publisher=RecordingAudioPublisher(),
        metric_rollups=worker_module.MetricRollupWindow(),
        worker_state_store=worker_state_store,
    )

    await session.handle_control_message({"type": "session.start"})
    session.session.active_response = True
    await session.handle_control_message({"type": "client.interrupt"})
    await session._emit_runtime_event(
        {
            "type": "metrics.update",
            "metrics": {"commit_to_first_audio_delta_ms": 12.5},
            "timestamps": {"t_audio_commit_sent": 1.0},
        }
    )

    metrics_payload = room.local_participant.published_payloads[-1]
    assert metrics_payload["type"] == "metrics.update"
    assert metrics_payload["participant_id"] == "browser-test"
    assert metrics_payload["session_id"] == "session-test"
    assert metrics_payload["metrics"]["assistant_queue_depth_ms"] == 0.0
    assert metrics_payload["metrics"]["interrupt_clear_ms"] is not None
    assert metrics_payload["rollups"]["interrupt_clear_last_ms"] is not None
    assert metrics_payload["rollups"]["interrupt_clear_p99_ms"] is not None
    assert metrics_payload["counters"]["interruption_count"] == 1


@pytest.mark.asyncio
async def test_livekit_duplicate_commit_is_ignored_after_turn_is_closed(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    worker_state_store = worker_module.LiveKitWorkerStateStore(
        path=str(tmp_path / "livekit-worker-state.json")
    )
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=FakeRoom(),
        audio_publisher=RecordingAudioPublisher(),
        worker_state_store=worker_state_store,
    )

    await session.handle_control_message({"type": "session.start"})
    await session.handle_control_message({"type": "client.speech.start"})
    await session.ingest_audio_frame(_pcm_frame(sample_rate=16000, samples=[1000] * 320))
    await session.handle_control_message({"type": "client.speech.commit"})
    await session.handle_control_message({"type": "client.speech.commit"})

    assert session.session.commit_calls == 1
    assert len(session.session.append_calls) == 1
    assert worker_state_store.snapshot["duplicate_commit_count"] == 1


@pytest.mark.asyncio
async def test_livekit_turn_ids_increment_monotonically_per_session(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    room = FakeRoom()
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=room,
        audio_publisher=RecordingAudioPublisher(),
    )

    await session.handle_control_message({"type": "session.start"})
    await session.handle_control_message({"type": "client.speech.start"})
    await session.handle_control_message({"type": "client.speech.commit"})
    await session.handle_control_message({"type": "client.speech.start"})
    await session.handle_control_message({"type": "client.speech.commit"})

    committed_payloads = [
        payload
        for payload in room.local_participant.published_payloads
        if payload["type"] == "turn.committed"
    ]

    assert session.session.turn_ids == ["1", "2"]
    assert [payload["turn_id"] for payload in committed_payloads] == ["1", "2"]


@pytest.mark.asyncio
async def test_livekit_browser_playback_telemetry_updates_session_metrics(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=FakeRoom(),
        audio_publisher=RecordingAudioPublisher(),
    )

    await session.handle_control_message({"type": "session.start"})
    session.response_turn_id = "1"

    await session.handle_control_message(
        {
            "type": "client.telemetry",
            "event_name": "assistant.playback.started",
            "turn_id": "1",
            "client_epoch_ms": 123456.0,
            "client_perf_now_ms": 789.0,
            "commit_to_first_audio_played_ms": 187.5,
        }
    )

    assert session.session.browser_first_audio_played_ms == 187.5
    assert session.session.flush_metrics_update_calls == 1


@pytest.mark.asyncio
async def test_livekit_browser_idle_timeout_emits_structured_error_and_cleans_session(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    room = FakeRoom()
    audio_publisher = RecordingAudioPublisher()
    worker_state_store = worker_module.LiveKitWorkerStateStore(
        path=str(tmp_path / "livekit-worker-state.json")
    )
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(LIVEKIT_BROWSER_IDLE_TIMEOUT_SECONDS=0.01),
        participant_identity="browser-test",
        room=room,
        audio_publisher=audio_publisher,
        worker_state_store=worker_state_store,
    )

    await session.handle_control_message({"type": "session.start"})
    await asyncio.sleep(0.03)

    assert session.started is False
    assert session.turn_state == worker_module.TurnLifecycleState.CLOSED
    assert audio_publisher.clear_calls >= 1
    assert room.local_participant.published_payloads[-1]["type"] == "error"
    assert room.local_participant.published_payloads[-1]["code"] == "BROWSER_IDLE_TIMEOUT"
    assert room.local_participant.published_payloads[-1]["reason"] == "browser_idle_timeout"
    assert room.local_participant.published_payloads[-1]["participant_id"] == "browser-test"
    assert room.local_participant.published_payloads[-1]["session_id"] == "session-test"
    assert worker_state_store.snapshot["error_count"] == 1


@pytest.mark.asyncio
async def test_livekit_real_publisher_path_does_not_deadlock_first_egress_metrics(monkeypatch) -> None:
    class BenchmarkRealtimeSession(session_module.RealtimeSession):
        def _build_qwen_realtime_client(self):
            return StubRealtimeQwenClient(
                output_sample_rate=self.settings.qwen_output_sample_rate,
            )

    monkeypatch.setattr(worker_module, "RealtimeSession", BenchmarkRealtimeSession)

    room = FakeRoom()
    audio_source = FakePublishingAudioSource(queue_size_ms=150)
    audio_publisher = worker_module.AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=10,
    )
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(LIVEKIT_INPUT_VAD_ENABLED=False),
        participant_identity="browser-test",
        room=room,
        audio_publisher=audio_publisher,
        metric_rollups=worker_module.MetricRollupWindow(),
    )

    try:
        await session.handle_control_message({"type": "session.start"})
        await session.handle_control_message({"type": "client.speech.start"})
        for _ in range(4):
            await session.ingest_audio_frame(_pcm_frame(sample_rate=16000, samples=[1000] * 320))
        await session.handle_control_message({"type": "client.speech.commit"})

        async def wait_for_assistant_done() -> None:
            for _ in range(200):
                if any(
                    payload.get("type") == "assistant.done"
                    for payload in room.local_participant.published_payloads
                ):
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("assistant.done was not published")

        await wait_for_assistant_done()

        async def wait_for_egress_metric() -> None:
            for _ in range(200):
                metrics_payloads = [
                    payload
                    for payload in room.local_participant.published_payloads
                    if payload["type"] == "metrics.update"
                ]
                if any(
                    isinstance(
                        payload["metrics"].get("commit_to_first_livekit_egress_ms"),
                        (int, float),
                    )
                    for payload in metrics_payloads
                ):
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("commit_to_first_livekit_egress_ms was not published")

        await wait_for_egress_metric()

        published_types = [payload["type"] for payload in room.local_participant.published_payloads]
        assert "assistant.audio_started" in published_types
        assert "assistant.done" in published_types
        metrics_payloads = [
            payload
            for payload in room.local_participant.published_payloads
            if payload["type"] == "metrics.update"
        ]
        assert metrics_payloads
        assert any(
            isinstance(payload["metrics"].get("commit_to_first_livekit_egress_ms"), (int, float))
            for payload in metrics_payloads
        )
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_livekit_vad_streams_audio_before_end_of_speech(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    room = FakeRoom()
    audio_publisher = RecordingAudioPublisher()
    vad_stream = FakeVADStream()
    settings = Settings(LIVEKIT_INPUT_VAD_PREFIX_PADDING_DURATION=0.2)
    session = worker_module.ParticipantBridgeSession(
        settings=settings,
        participant_identity="browser-test",
        room=room,
        audio_publisher=audio_publisher,
        metric_rollups=worker_module.MetricRollupWindow(),
        input_vad=FakeVAD(vad_stream),
    )

    await session.handle_control_message({"type": "session.start"})
    for _ in range(2):
        await session.ingest_audio_frame(_pcm_frame(sample_rate=16000, samples=[0] * 320))
    vad_stream.emit(
        worker_module.VADEvent(
            type=worker_module.VADEventType.START_OF_SPEECH,
            samples_index=0,
            timestamp=0.0,
            speech_duration=0.05,
            silence_duration=0.0,
            frames=[],
            speaking=True,
        )
    )
    await asyncio.sleep(0)
    assert len(session.session.append_calls) == 0

    for _ in range(3):
        await session.ingest_audio_frame(_pcm_frame(sample_rate=16000, samples=[1000] * 320))
    assert len(session.session.append_calls) == 1

    vad_stream.emit(
        worker_module.VADEvent(
            type=worker_module.VADEventType.END_OF_SPEECH,
            samples_index=0,
            timestamp=0.0,
            speech_duration=0.06,
            silence_duration=0.35,
            frames=[],
            speaking=False,
        )
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    try:
        assert session.session.commit_calls == 1
        assert len(session.session.append_calls) == 2
        published_types = [payload["type"] for payload in room.local_participant.published_payloads]
        assert published_types == [
            "turn.started",
            "turn.committed",
            "assistant.audio_started",
            "assistant.done",
        ]
        commit_payload = room.local_participant.published_payloads[1]
        assert commit_payload["input_audio_ms"] == 100.0
        assert commit_payload["rollups"]["input_audio_last_ms"] == 100.0
        assert commit_payload["rollups"]["speech_duration_last_ms"] == 60.0
        assert all(payload["output_version"] == 1 for payload in room.local_participant.published_payloads)
        assert all(payload["participant_id"] == "browser-test" for payload in room.local_participant.published_payloads)
        assert all(payload["session_id"] == "session-test" for payload in room.local_participant.published_payloads)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_livekit_caps_buffered_pre_audio_payloads(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)
    monkeypatch.setattr(worker_module, "MAX_PENDING_PRE_AUDIO_PAYLOADS", 3)

    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=FakeRoom(),
        audio_publisher=RecordingAudioPublisher(),
    )

    try:
        await session.handle_control_message({"type": "session.start"})
        await session.handle_control_message({"type": "client.speech.start"})

        for index in range(4):
            await session._emit_runtime_event(
                {
                    "type": "metrics.update",
                    "metrics": {"commit_to_first_audio_delta_ms": float(index)},
                    "timestamps": {"t_audio_commit_sent": float(index)},
                }
            )

        assert len(session.pending_pre_audio_payloads) == 3
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_livekit_max_turn_auto_commits_once_and_drops_tail_until_vad_end(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    room = FakeRoom()
    vad_stream = FakeVADStream()
    long_frame = _pcm_frame(sample_rate=16000, samples=[1000] * 8000)
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(LIVEKIT_MAX_TURN_MS=1000),
        participant_identity="browser-test",
        room=room,
        audio_publisher=RecordingAudioPublisher(),
        input_vad=FakeVAD(vad_stream),
    )

    await session.handle_control_message({"type": "session.start"})
    vad_stream.emit(
        worker_module.VADEvent(
            type=worker_module.VADEventType.START_OF_SPEECH,
            samples_index=0,
            timestamp=0.0,
            speech_duration=0.05,
            silence_duration=0.0,
            frames=[],
            speaking=True,
        )
    )
    await asyncio.sleep(0)

    try:
        await session.ingest_audio_frame(long_frame)
        await session.ingest_audio_frame(long_frame)

        assert session.session.commit_calls == 1
        assert session.drop_audio_until_turn_boundary is True
        published_types = [payload["type"] for payload in room.local_participant.published_payloads]
        assert "input.speech.truncated" in published_types

        append_calls_after_commit = len(session.session.append_calls)
        await session.ingest_audio_frame(long_frame)
        assert len(session.session.append_calls) == append_calls_after_commit

        vad_stream.emit(
            worker_module.VADEvent(
                type=worker_module.VADEventType.END_OF_SPEECH,
                samples_index=0,
                timestamp=0.0,
                speech_duration=0.2,
                silence_duration=0.35,
                frames=[],
                speaking=False,
            )
        )
        await asyncio.sleep(0)
        assert session.drop_audio_until_turn_boundary is False
        assert session.session.commit_calls == 1

        vad_stream.emit(
            worker_module.VADEvent(
                type=worker_module.VADEventType.START_OF_SPEECH,
                samples_index=0,
                timestamp=0.0,
                speech_duration=0.05,
                silence_duration=0.0,
                frames=[],
                speaking=True,
            )
        )
        await asyncio.sleep(0)
        await session.ingest_audio_frame(long_frame)
        assert len(session.session.append_calls) > append_calls_after_commit
    finally:
        await session.close()


def test_configure_worker_logging_skips_artifact_file_when_disabled(monkeypatch, tmp_path) -> None:
    basic_config_calls: list[dict] = []
    file_handler_calls: list[tuple[object, str, str | None]] = []

    class FakeFileHandler(logging.Handler):
        def __init__(self, filename, mode="a", encoding=None) -> None:
            super().__init__()
            file_handler_calls.append((filename, mode, encoding))

    monkeypatch.setattr(worker_module.logging, "basicConfig", lambda **kwargs: basic_config_calls.append(kwargs))
    monkeypatch.setattr(worker_module.logging, "FileHandler", FakeFileHandler)

    settings = Settings(
        AUDIO_ARTIFACT_LOGGING_ENABLED=False,
        AUDIO_ARTIFACT_LOG_PATH=str(tmp_path / "logs" / "audio-artifacts.log"),
    )

    worker_module.configure_worker_logging(settings)

    assert basic_config_calls
    assert file_handler_calls == []
    assert not (tmp_path / "logs" / "audio-artifacts.log").exists()


def test_configure_worker_logging_enables_artifact_file_when_requested(monkeypatch, tmp_path) -> None:
    basic_config_calls: list[dict] = []
    file_handler_calls: list[tuple[object, str, str | None]] = []
    output_logger = logging.getLogger("src.livekit.output")
    worker_logger = logging.getLogger("src.livekit.worker")
    original_output_handlers = list(output_logger.handlers)
    original_worker_handlers = list(worker_logger.handlers)

    class FakeFileHandler(logging.Handler):
        def __init__(self, filename, mode="a", encoding=None) -> None:
            super().__init__()
            file_handler_calls.append((filename, mode, encoding))

    monkeypatch.setattr(worker_module.logging, "basicConfig", lambda **kwargs: basic_config_calls.append(kwargs))
    monkeypatch.setattr(worker_module.logging, "FileHandler", FakeFileHandler)

    settings = Settings(
        AUDIO_ARTIFACT_LOGGING_ENABLED=True,
        AUDIO_ARTIFACT_LOG_PATH=str(tmp_path / "logs" / "audio-artifacts.log"),
    )

    try:
        worker_module.configure_worker_logging(settings)

        assert basic_config_calls
        assert len(file_handler_calls) == 1
        assert str(file_handler_calls[0][0]).endswith("audio-artifacts.log")
        assert (tmp_path / "logs").exists()
        assert any(isinstance(handler, FakeFileHandler) for handler in output_logger.handlers)
        assert any(isinstance(handler, FakeFileHandler) for handler in worker_logger.handlers)
    finally:
        output_logger.handlers = original_output_handlers
        worker_logger.handlers = original_worker_handlers
