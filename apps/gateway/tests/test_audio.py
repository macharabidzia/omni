import base64

import pytest

from src.realtime.audio import AudioValidationError, validate_audio_chunk


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

