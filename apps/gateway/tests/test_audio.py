import base64

import pytest

from src.realtime.audio import (
    AudioValidationError,
    iter_pcm16_chunks,
    pcm16_peak_abs,
    pcm16_rms,
    pcm16_samples,
    pcm16_to_wav,
    validate_audio_bytes,
    validate_audio_chunk,
    wav_to_pcm16,
)


def test_validate_audio_chunk_accepts_valid_80ms_pcm16() -> None:
    audio_bytes = b"\x00\x00" * 1280
    encoded = base64.b64encode(audio_bytes).decode("ascii")

    result = validate_audio_chunk(
        audio_base64=encoded,
        sample_rate=16000,
        channels=1,
        audio_format="pcm16",
        allowed_chunk_ms=(20, 40, 80, 120, 200),
    )

    assert result.duration_ms == 80
    assert result.sample_count == 1280


def test_validate_audio_chunk_rejects_invalid_duration() -> None:
    audio_bytes = b"\x00\x00" * 1000
    encoded = base64.b64encode(audio_bytes).decode("ascii")

    with pytest.raises(AudioValidationError) as exc:
        validate_audio_chunk(
            audio_base64=encoded,
            sample_rate=16000,
            channels=1,
            audio_format="pcm16",
            allowed_chunk_ms=(20, 40, 80, 120, 200),
        )

    assert exc.value.code == "INVALID_CHUNK_DURATION"


def test_validate_audio_bytes_accepts_internal_pcm16_chunk() -> None:
    audio_bytes = b"\x01\x00" * 640

    result = validate_audio_bytes(
        audio_bytes=audio_bytes,
        sample_rate=16000,
        channels=1,
        audio_format="pcm16",
        allowed_chunk_ms=(20, 40, 80, 120, 200),
    )

    assert result.audio_base64 is None
    assert result.audio_bytes == audio_bytes
    assert result.duration_ms == 40
    assert result.sample_count == 640


def test_iter_pcm16_chunks_pads_final_chunk_when_requested() -> None:
    audio_bytes = (b"\x01\x00" * 640) + (b"\x02\x00" * 400)

    chunks = list(iter_pcm16_chunks(audio_bytes, duration_ms=40, pad_final_chunk=True))

    assert len(chunks) == 2
    assert len(chunks[0]) == 1280
    assert len(chunks[1]) == 1280
    assert chunks[1][:800] == b"\x02\x00" * 400
    assert chunks[1][800:] == b"\x00" * 480


def test_iter_pcm16_chunks_rejects_unaligned_pcm16_bytes() -> None:
    with pytest.raises(ValueError, match="2-byte aligned"):
        list(iter_pcm16_chunks(b"\x00", duration_ms=40))


def test_pcm16_numpy_helpers_handle_signed_extremes() -> None:
    audio_bytes = b"\x00\x80\xff\x7f\x00\x00"

    samples = pcm16_samples(audio_bytes)

    assert samples.tolist() == [-32768, 32767, 0]
    assert pcm16_peak_abs(audio_bytes) == 32768
    assert pcm16_rms(audio_bytes) > 26700


def test_pcm16_to_wav_round_trips_back_to_pcm16() -> None:
    audio_bytes = (b"\x01\x00" * 320) + (b"\xff\xff" * 320)

    wav_bytes = pcm16_to_wav(audio_bytes, sample_rate=24000)
    restored_bytes, sample_rate = wav_to_pcm16(wav_bytes)

    assert sample_rate == 24000
    assert restored_bytes == audio_bytes


def test_wav_to_pcm16_rejects_invalid_wav() -> None:
    with pytest.raises(ValueError, match="Invalid WAV audio"):
        wav_to_pcm16(b"not-a-wav")
