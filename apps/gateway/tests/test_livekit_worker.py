import asyncio
import base64
import json
from array import array

import pytest

import src.livekit.worker as worker_module
from src.config import Settings


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

    async def publish_data(
        self,
        payload: str,
        *,
        reliable: bool,
        destination_identities: list[str],
        topic: str,
    ) -> None:
        assert reliable is True
        assert destination_identities
        assert topic
        self.published_payloads.append(json.loads(payload))


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


class RecordingAudioPublisher:
    def __init__(self) -> None:
        self.enqueued_audio: list[tuple[str, int]] = []
        self.finalize_calls = 0
        self.clear_calls = 0
        self.queued_duration = 0.0

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


class FakeRealtimeSession:
    def __init__(self, *, settings: Settings, emit_event) -> None:
        del settings
        self.emit_event = emit_event
        self.livekit_egress_marks = 0
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

    def mark_livekit_egress_started(self) -> None:
        self.livekit_egress_marks += 1

    def has_active_response(self) -> bool:
        return self.active_response


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
        self._yielded = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
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


def test_livekit_output_defaults_match_supported_transport() -> None:
    settings = Settings()

    assert settings.livekit_input_sample_rate == 48000
    assert settings.livekit_output_sample_rate == 48000
    assert settings.qwen_input_sample_rate == 16000
    assert settings.qwen_output_sample_rate == 24000
    assert settings.livekit_output_frame_ms == 20
    assert 20 <= settings.livekit_output_queue_ms <= 80
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
        "transcript.delta",
        "metrics.update",
        "assistant.done",
    ]


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
    assert session.session.append_calls[0]["sample_rate"] == 16000


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
        "transcript.delta",
        "assistant.audio.metadata",
        "assistant.done",
    ]
    metadata_payload = room.local_participant.published_payloads[1]
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


@pytest.mark.asyncio
async def test_livekit_vad_commits_trimmed_speech_segment(monkeypatch) -> None:
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

    prefix_samples = [0] * 3200
    speech_samples = [1000] * 1600
    trailing_silence_samples = [0] * 5600
    session.vad_turn_audio.extend(array("h", prefix_samples + speech_samples + trailing_silence_samples).tobytes())
    vad_stream.emit(
        worker_module.VADEvent(
            type=worker_module.VADEventType.END_OF_SPEECH,
            samples_index=0,
            timestamp=0.0,
            speech_duration=0.1,
            silence_duration=0.35,
            frames=[],
            speaking=False,
        )
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    try:
        assert session.session.commit_calls == 1
        assert len(session.session.append_calls) == 15
        published_types = [payload["type"] for payload in room.local_participant.published_payloads]
        assert published_types == [
            "input.speech.start",
            "input.speech.commit",
            "assistant.done",
        ]
    finally:
        await session.close()
