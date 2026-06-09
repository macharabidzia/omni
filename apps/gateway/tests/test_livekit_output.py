import base64
from array import array

import pytest

from src.livekit.output import AssistantAudioPublisher


class FakeAudioSource:
    def __init__(self, *, queue_size_ms: int = 40, queued_duration: float = 0.0) -> None:
        self.frames = []
        self.cleared = False
        self.queue_size_ms = queue_size_ms
        self._queued_duration = queued_duration

    async def capture_frame(self, frame) -> None:
        self.frames.append(frame)

    def clear_queue(self) -> None:
        self.cleared = True

    async def wait_for_playout(self) -> None:
        return None

    @property
    def queued_duration(self) -> float:
        return self._queued_duration


def _constant_pcm16_base64(*, samples: int, amplitude: int) -> str:
    pcm16 = array("h", [amplitude] * samples)
    return base64.b64encode(pcm16.tobytes()).decode("ascii")


def _pcm16_base64(values: list[int]) -> str:
    pcm16 = array("h", values)
    return base64.b64encode(pcm16.tobytes()).decode("ascii")


@pytest.mark.asyncio
async def test_assistant_audio_publisher_resamples_to_48k_and_fades_partial_tail() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    # 10 ms at 24 kHz -> 10 ms at 48 kHz after resampling, which leaves a half-frame tail.
    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=240, amplitude=1000),
        input_sample_rate=24000,
    )
    await publisher.finalize_turn()

    assert len(audio_source.frames) == 1
    frame = audio_source.frames[0]
    assert frame.sample_rate == 48000
    assert frame.num_channels == 1
    assert frame.samples_per_channel == 960

    samples = array("h")
    samples.frombytes(bytes(frame.data))
    assert len(samples) == 960
    tail = samples[480:]
    assert max(abs(sample) for sample in tail) > 0
    assert tail[-1] == 0


@pytest.mark.asyncio
async def test_assistant_audio_publisher_clear_resets_state() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=240, amplitude=1000),
        input_sample_rate=24000,
    )
    await publisher.clear()

    assert audio_source.cleared is True
    assert audio_source.frames == []


@pytest.mark.asyncio
async def test_assistant_audio_publisher_calls_frame_sink_for_complete_frames() -> None:
    audio_source = FakeAudioSource()
    frame_payloads: list[bytes] = []

    async def frame_sink(frame_bytes: bytes) -> None:
        frame_payloads.append(frame_bytes)

    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
        frame_sink=frame_sink,
    )

    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=480, amplitude=1000),
        input_sample_rate=24000,
    )
    await publisher.finalize_turn()

    assert len(audio_source.frames) == 1
    assert len(frame_payloads) == 1
    assert frame_payloads[0] == bytes(audio_source.frames[0].data)


@pytest.mark.asyncio
async def test_assistant_audio_publisher_emits_stable_frames_from_uneven_chunks() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=10,
    )

    for sample_count in (240, 360, 600, 120):
        await publisher.enqueue_base64(
            _constant_pcm16_base64(samples=sample_count, amplitude=1_000),
            input_sample_rate=48000,
        )
    await publisher.finalize_turn()

    assert [frame.samples_per_channel for frame in audio_source.frames] == [480, 480, 480]
    assert all(len(bytes(frame.data)) == 960 for frame in audio_source.frames)


@pytest.mark.asyncio
async def test_assistant_audio_publisher_trims_turn_leading_silence() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    leading_silence_samples = int(48000 * 0.06)
    voiced_samples = int(48000 * 0.04)
    await publisher.enqueue_base64(
        _pcm16_base64(([0] * leading_silence_samples) + ([1000] * voiced_samples)),
        input_sample_rate=48000,
    )
    await publisher.finalize_turn()

    assert len(audio_source.frames) == 2

    first_frame_samples = array("h")
    first_frame_samples.frombytes(bytes(audio_source.frames[0].data))
    assert max(abs(sample) for sample in first_frame_samples[:480]) >= 500


@pytest.mark.asyncio
async def test_assistant_audio_publisher_applies_fade_in_after_leading_trim() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    leading_silence_samples = int(48000 * 0.06)
    voiced_samples = int(48000 * 0.04)
    await publisher.enqueue_base64(
        _pcm16_base64(([0] * leading_silence_samples) + ([4_000] * voiced_samples)),
        input_sample_rate=48000,
    )
    await publisher.finalize_turn()

    first_frame_samples = array("h")
    first_frame_samples.frombytes(bytes(audio_source.frames[0].data))
    non_zero_samples = [sample for sample in first_frame_samples if sample != 0]

    assert non_zero_samples
    assert abs(non_zero_samples[0]) < 4_000
    assert abs(non_zero_samples[min(len(non_zero_samples) - 1, 16)]) >= abs(non_zero_samples[0])


@pytest.mark.asyncio
async def test_assistant_audio_publisher_smooths_large_chunk_boundary_jump() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=480, amplitude=12_000),
        input_sample_rate=48000,
    )
    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=480, amplitude=-12_000),
        input_sample_rate=48000,
    )
    await publisher.finalize_turn()

    assert len(audio_source.frames) == 1
    samples = array("h")
    samples.frombytes(bytes(audio_source.frames[0].data))
    assert samples[479] == 12_000
    assert samples[480] > -12_000
    assert abs(samples[480] - samples[479]) < 24_000


@pytest.mark.asyncio
async def test_assistant_audio_publisher_does_not_apply_boundary_smoothing_when_it_worsens_transition() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    await publisher.enqueue_base64(
        _pcm16_base64(([1000] * 959) + [0]),
        input_sample_rate=48000,
    )
    oscillating_start = [6000 if index % 2 == 0 else -6000 for index in range(144)]
    await publisher.enqueue_base64(
        _pcm16_base64(oscillating_start + ([0] * (960 - len(oscillating_start)))),
        input_sample_rate=48000,
    )
    await publisher.finalize_turn()

    samples = array("h")
    samples.frombytes(b"".join(bytes(frame.data) for frame in audio_source.frames))
    boundary_index = 960
    raw_boundary_jump = abs(samples[boundary_index] - samples[boundary_index - 1])
    transition = samples[boundary_index : boundary_index + len(oscillating_start)]
    max_transition_jump = max(
        abs(transition[index] - transition[index - 1])
        for index in range(1, len(transition))
    )

    assert raw_boundary_jump == 6000
    assert samples[boundary_index] == 6000
    assert max_transition_jump <= 12000


@pytest.mark.asyncio
async def test_assistant_audio_publisher_smooths_isolated_spike() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    values = [1_000] * 960
    values[480] = 20_000
    await publisher.enqueue_base64(
        _pcm16_base64(values),
        input_sample_rate=48000,
    )
    await publisher.finalize_turn()

    assert len(audio_source.frames) == 1
    samples = array("h")
    samples.frombytes(bytes(audio_source.frames[0].data))
    assert abs(samples[480] - 1_000) < 500


@pytest.mark.asyncio
async def test_assistant_audio_publisher_preserves_short_internal_silence_after_voice_started() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=960, amplitude=1_000),
        input_sample_rate=48000,
    )
    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=960, amplitude=0),
        input_sample_rate=48000,
    )
    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=960, amplitude=1_000),
        input_sample_rate=48000,
    )
    await publisher.finalize_turn()

    assert len(audio_source.frames) == 3

    silent_frame_samples = array("h")
    silent_frame_samples.frombytes(bytes(audio_source.frames[1].data))
    assert max(abs(sample) for sample in silent_frame_samples) == 0

    third_frame_samples = array("h")
    third_frame_samples.frombytes(bytes(audio_source.frames[2].data))
    assert max(abs(sample) for sample in third_frame_samples) >= 500


@pytest.mark.asyncio
async def test_assistant_audio_publisher_logs_queue_backpressure_warning(caplog) -> None:
    audio_source = FakeAudioSource(queue_size_ms=40, queued_duration=0.04)
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    with caplog.at_level("WARNING"):
        await publisher.enqueue_base64(
            _constant_pcm16_base64(samples=960, amplitude=1_000),
            input_sample_rate=48000,
        )

    assert "Assistant audio queue backpressure" in caplog.text


@pytest.mark.asyncio
async def test_assistant_audio_publisher_drops_pending_frames_at_hard_queue_cap(caplog) -> None:
    audio_source = FakeAudioSource(queue_size_ms=20, queued_duration=0.02)
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    with caplog.at_level("WARNING"):
        await publisher.enqueue_base64(
            _constant_pcm16_base64(samples=1920, amplitude=1_000),
            input_sample_rate=48000,
        )

    assert len(audio_source.frames) == 1
    assert "dropping queued frame due to hard queue cap" in caplog.text


@pytest.mark.asyncio
async def test_assistant_audio_publisher_rejects_mid_turn_sample_rate_change() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=480, amplitude=1_000),
        input_sample_rate=24000,
    )

    with pytest.raises(ValueError, match="sample rate changed mid-turn"):
        await publisher.enqueue_base64(
            _constant_pcm16_base64(samples=960, amplitude=1_000),
            input_sample_rate=48000,
        )


@pytest.mark.asyncio
async def test_assistant_audio_publisher_trims_post_start_chunk_leading_silence() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=960, amplitude=1_000),
        input_sample_rate=48000,
    )
    await publisher.enqueue_base64(
        _pcm16_base64(([0] * 1440) + ([1_000] * 480)),
        input_sample_rate=48000,
    )
    await publisher.finalize_turn()

    assert len(audio_source.frames) == 2
    second_frame_samples = array("h")
    second_frame_samples.frombytes(bytes(audio_source.frames[1].data))
    assert max(abs(sample) for sample in second_frame_samples[:480]) >= 500


@pytest.mark.asyncio
async def test_assistant_audio_publisher_trims_post_start_chunk_trailing_silence() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
    )

    await publisher.enqueue_base64(
        _constant_pcm16_base64(samples=960, amplitude=1_000),
        input_sample_rate=48000,
    )
    await publisher.enqueue_base64(
        _pcm16_base64(([1_000] * 480) + ([0] * 1440)),
        input_sample_rate=48000,
    )
    await publisher.finalize_turn()

    assert len(audio_source.frames) == 2
    second_frame_samples = array("h")
    second_frame_samples.frombytes(bytes(audio_source.frames[1].data))
    assert max(abs(sample) for sample in second_frame_samples[:480]) >= 500
    assert max(abs(sample) for sample in second_frame_samples[480:]) < 1_000
    assert second_frame_samples[-1] == 0
