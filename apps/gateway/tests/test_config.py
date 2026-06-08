import pytest
from pydantic import ValidationError

from src.config import Settings


def test_settings_parse_comma_separated_cors_allow_origins() -> None:
    settings = Settings(CORS_ALLOW_ORIGINS="http://localhost:5173, http://127.0.0.1:5173")

    assert settings.cors_allow_origins == (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )


def test_settings_allow_empty_cors_allow_origins() -> None:
    settings = Settings(CORS_ALLOW_ORIGINS="")

    assert settings.cors_allow_origins == ()


def test_settings_default_audio_artifact_log_path_uses_workspace() -> None:
    settings = Settings()

    assert settings.audio_artifact_log_path.endswith("/tmp/logs/audio-artifacts.log")


def test_settings_default_qwen_audio_backend() -> None:
    settings = Settings()

    assert settings.qwen_audio_backend == "realtime"


def test_settings_default_audio_rates_separate_model_from_livekit() -> None:
    settings = Settings()

    assert settings.livekit_input_sample_rate == 48000
    assert settings.livekit_output_sample_rate == 48000
    assert settings.qwen_input_sample_rate == 16000
    assert settings.qwen_output_sample_rate == 24000
    assert settings.livekit_preroll_frames == 1


def test_settings_parse_preferred_qwen_audio_rate_env_names() -> None:
    settings = Settings(
        QWEN_AUDIO_INPUT_SAMPLE_RATE=16000,
        QWEN_AUDIO_OUTPUT_SAMPLE_RATE=24000,
        LIVEKIT_INPUT_SAMPLE_RATE=48000,
        LIVEKIT_OUTPUT_SAMPLE_RATE=48000,
    )

    assert settings.qwen_input_sample_rate == 16000
    assert settings.qwen_output_sample_rate == 24000


def test_settings_reject_livekit_publish_rate_equal_to_qwen_output_rate() -> None:
    with pytest.raises(ValidationError, match="LIVEKIT_OUTPUT_SAMPLE_RATE"):
        Settings(
            QWEN_AUDIO_INPUT_SAMPLE_RATE=16000,
            QWEN_AUDIO_OUTPUT_SAMPLE_RATE=24000,
            LIVEKIT_INPUT_SAMPLE_RATE=48000,
            LIVEKIT_OUTPUT_SAMPLE_RATE=24000,
        )


def test_settings_reject_livekit_input_rate_equal_to_qwen_input_rate() -> None:
    with pytest.raises(ValidationError, match="LIVEKIT_INPUT_SAMPLE_RATE"):
        Settings(
            QWEN_AUDIO_INPUT_SAMPLE_RATE=16000,
            QWEN_AUDIO_OUTPUT_SAMPLE_RATE=24000,
            LIVEKIT_INPUT_SAMPLE_RATE=16000,
            LIVEKIT_OUTPUT_SAMPLE_RATE=48000,
        )


def test_settings_reject_non_native_qwen_output_rate() -> None:
    with pytest.raises(ValidationError, match="QWEN_AUDIO_OUTPUT_SAMPLE_RATE"):
        Settings(
            QWEN_AUDIO_INPUT_SAMPLE_RATE=16000,
            QWEN_AUDIO_OUTPUT_SAMPLE_RATE=48000,
            LIVEKIT_INPUT_SAMPLE_RATE=48000,
            LIVEKIT_OUTPUT_SAMPLE_RATE=48000,
        )


def test_settings_default_livekit_input_vad() -> None:
    settings = Settings()

    assert settings.livekit_input_vad_enabled is True
    assert settings.livekit_input_vad_min_speech_duration == 0.05
    assert settings.livekit_input_vad_min_silence_duration == 0.35
    assert settings.livekit_input_vad_prefix_padding_duration == 0.15
    assert settings.livekit_input_vad_activation_threshold == 0.5
    assert settings.livekit_input_vad_force_cpu is True
