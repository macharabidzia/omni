import base64
import json

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
        self.published_binary_payloads: list[bytes] = []

    async def publish_data(
        self,
        payload: bytes | str,
        *,
        reliable: bool,
        destination_identities: list[str],
        topic: str,
    ) -> None:
        assert reliable is True
        assert destination_identities
        assert topic
        if isinstance(payload, bytes):
            self.published_binary_payloads.append(payload)
            return
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

    async def enqueue_base64(self, audio_base64: str, *, input_sample_rate: int) -> None:
        self.enqueued_audio.append((audio_base64, input_sample_rate))

    async def finalize_turn(self) -> None:
        self.finalize_calls += 1

    async def clear(self) -> None:
        self.clear_calls += 1


class FakeRealtimeSession:
    def __init__(self, *, settings: Settings, emit_event) -> None:
        del settings
        self.emit_event = emit_event

    async def start_session(self, _payload: dict) -> None:
        return None

    async def append_audio_chunk(self, **_kwargs) -> None:
        return None

    async def commit_audio(self) -> None:
        audio_base64 = base64.b64encode(b"\x00\x00" * 960).decode("ascii")
        await self.emit_event(
            {
                "type": "assistant.audio.delta",
                "audio_base64": audio_base64,
                "sample_rate": 24000,
                "channels": 1,
                "format": "pcm16",
            }
        )
        await self.emit_event({"type": "assistant.done"})

    async def cancel_response(self) -> None:
        return None

    async def close(self) -> None:
        return None


def test_livekit_worker_uses_configured_audio_queue(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.rtc, "Room", FakeRoom)
    monkeypatch.setattr(worker_module.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(worker_module, "AssistantAudioPublisher", FakeAudioPublisher)

    settings = Settings(
        LIVEKIT_OUTPUT_SAMPLE_RATE=24000,
        LIVEKIT_OUTPUT_FRAME_MS=10,
        LIVEKIT_OUTPUT_QUEUE_MS=120,
    )

    worker = worker_module.LiveKitWorker(settings)

    assert worker.audio_source.sample_rate == 24000
    assert worker.audio_source.num_channels == 1
    assert worker.audio_source.queue_size_ms == 120
    assert worker.audio_publisher.output_sample_rate == 24000
    assert worker.audio_publisher.output_frame_ms == 10


def test_livekit_output_defaults_match_supported_transport() -> None:
    settings = Settings()

    assert settings.livekit_output_sample_rate == 48000
    assert settings.livekit_output_frame_ms == 20
    assert 20 <= settings.livekit_output_queue_ms <= 80


@pytest.mark.asyncio
async def test_livekit_worker_publishes_pcm_audio_packet(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.rtc, "Room", FakeRoom)
    monkeypatch.setattr(worker_module.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(worker_module, "AssistantAudioPublisher", FakeAudioPublisher)

    worker = worker_module.LiveKitWorker(Settings())
    worker.active_participant_identity = "browser-test"

    await worker._publish_audio_packet(b"\x01\x02\x03\x04")

    assert len(worker.room.local_participant.published_binary_payloads) == 1
    payload = worker.room.local_participant.published_binary_payloads[0]
    assert payload[0] == 1
    assert int.from_bytes(payload[1:5], byteorder="big") == worker.settings.livekit_output_sample_rate
    assert payload[5] == 1
    assert payload[6:] == b"\x01\x02\x03\x04"


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
    assert audio_publisher.finalize_calls == 1
    published_types = [payload["type"] for payload in room.local_participant.published_payloads]
    assert published_types == [
        "transcript.delta",
        "metrics.update",
        "assistant.done",
    ]


@pytest.mark.asyncio
async def test_livekit_commit_forwards_audio_delta_only_in_debug_mode(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "RealtimeSession", FakeRealtimeSession)

    room = FakeRoom()
    audio_publisher = RecordingAudioPublisher()
    session = worker_module.ParticipantBridgeSession(
        settings=Settings(),
        participant_identity="browser-test",
        room=room,
        audio_publisher=audio_publisher,
    )

    await session.handle_control_message({"type": "session.start", "debug_audio_deltas": True})
    await session.handle_control_message({"type": "client.speech.start"})
    await session._emit_runtime_event({"type": "transcript.delta", "text": "hello"})

    await session.handle_control_message({"type": "client.speech.commit"})

    assert len(audio_publisher.enqueued_audio) == 1
    published_types = [payload["type"] for payload in room.local_participant.published_payloads]
    assert published_types == [
        "transcript.delta",
        "assistant.audio.delta",
        "assistant.done",
    ]
