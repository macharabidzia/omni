import base64

import pytest

from src.realtime.audio import AudioValidationError, iter_pcm16_chunks, validate_audio_chunk


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
