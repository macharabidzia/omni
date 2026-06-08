import base64
from array import array

import pytest

from src.livekit.output import AssistantAudioPublisher


class FakeAudioSource:
    def __init__(self) -> None:
        self.frames = []
        self.cleared = False

    async def capture_frame(self, frame) -> None:
        self.frames.append(frame)

    def clear_queue(self) -> None:
        self.cleared = True

    async def wait_for_playout(self) -> None:
        return None

    @property
    def queued_duration(self) -> float:
        return 0.0


def _constant_pcm16_base64(*, samples: int, amplitude: int) -> str:
    pcm16 = array("h", [amplitude] * samples)
    return base64.b64encode(pcm16.tobytes()).decode("ascii")


def _pcm16_base64(values: list[int]) -> str:
    pcm16 = array("h", values)
    return base64.b64encode(pcm16.tobytes()).decode("ascii")


def _mean_abs_diff(values: array) -> float:
    if len(values) < 2:
        return 0.0
    return sum(abs(values[index] - values[index - 1]) for index in range(1, len(values))) / (
        len(values) - 1
    )


@pytest.mark.asyncio
async def test_assistant_audio_publisher_resamples_to_48k_and_fades_partial_tail() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
        output_lowpass_hz=6000,
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
        output_lowpass_hz=6000,
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
        output_lowpass_hz=6000,
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
async def test_assistant_audio_publisher_trims_turn_leading_silence() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
        output_lowpass_hz=6000,
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
async def test_assistant_audio_publisher_smooths_large_chunk_boundary_jump() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
        output_lowpass_hz=0,
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
async def test_assistant_audio_publisher_smooths_isolated_spike() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
        output_lowpass_hz=0,
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
async def test_assistant_audio_publisher_lowpass_reduces_high_frequency_roughness() -> None:
    audio_source = FakeAudioSource()
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=48000,
        output_frame_ms=20,
        output_lowpass_hz=2000,
    )

    input_values = [1_000 if index % 2 == 0 else -1_000 for index in range(960)]
    await publisher.enqueue_base64(
        _pcm16_base64(input_values),
        input_sample_rate=48000,
    )
    await publisher.finalize_turn()

    assert len(audio_source.frames) == 1
    output_samples = array("h")
    output_samples.frombytes(bytes(audio_source.frames[0].data))
    assert _mean_abs_diff(output_samples) < _mean_abs_diff(array("h", input_values))
