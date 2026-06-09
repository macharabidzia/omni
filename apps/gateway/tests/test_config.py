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


def test_settings_allow_wildcard_cors_outside_production() -> None:
    settings = Settings(GATEWAY_ENV="development", CORS_ALLOW_ORIGINS="*")

    assert settings.cors_allow_origins == ("*",)


def test_settings_reject_wildcard_cors_in_production() -> None:
    with pytest.raises(ValidationError, match="CORS_ALLOW_ORIGINS"):
        Settings(GATEWAY_ENV="production", CORS_ALLOW_ORIGINS="*")


def test_settings_default_audio_artifact_log_path_uses_workspace() -> None:
    settings = Settings()

    assert settings.audio_artifact_logging_enabled is False
    assert settings.audio_artifact_log_path.endswith("/tmp/logs/audio-artifacts.log")


def test_settings_default_livekit_worker_state_path_uses_workspace() -> None:
    settings = Settings()

    assert settings.livekit_worker_state_path.endswith("/tmp/state/livekit-worker-state.json")


def test_settings_default_qwen_audio_backend() -> None:
    settings = Settings()

    assert settings.qwen_audio_backend == "realtime"
    assert settings.livekit_qwen_input_chunk_ms == 80
    assert settings.qwen_prewarm_on_startup is False


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


def test_settings_parse_architecture_alias_env_names() -> None:
    settings = Settings(
        LIVEKIT_AGENT_IDENTITY="worker-alpha",
        LIVEKIT_INPUT_SAMPLE_RATE_HZ=48000,
        LIVEKIT_PUBLISH_SAMPLE_RATE_HZ=48000,
        LIVEKIT_AUDIO_SOURCE_QUEUE_MS=180,
        QWEN_INPUT_SAMPLE_RATE_HZ=16000,
        QWEN_OUTPUT_SAMPLE_RATE_HZ=24000,
        VAD_ENABLED=True,
        VAD_MIN_SPEECH_MS=80,
        VAD_MIN_SILENCE_MS=150,
        VAD_PREROLL_MS=200,
        DEBUG_AUDIO_ARTIFACTS=True,
        DEBUG_RAW_QWEN_EVENTS=True,
    )

    assert settings.livekit_agent_id == "worker-alpha"
    assert settings.livekit_input_sample_rate == 48000
    assert settings.livekit_output_sample_rate == 48000
    assert settings.livekit_output_queue_ms == 180
    assert settings.qwen_input_sample_rate == 16000
    assert settings.qwen_output_sample_rate == 24000
    assert settings.livekit_input_vad_enabled is True
    assert settings.livekit_input_vad_min_speech_duration == 0.08
    assert settings.livekit_input_vad_min_silence_duration == 0.15
    assert settings.livekit_input_vad_prefix_padding_duration == 0.2
    assert settings.audio_artifact_logging_enabled is True
    assert settings.qwen_debug_raw_events is True


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
    assert settings.livekit_input_vad_min_speech_duration == 0.08
    assert settings.livekit_input_vad_min_silence_duration == 0.15
    assert settings.livekit_input_vad_prefix_padding_duration == 0.2
    assert settings.livekit_input_vad_activation_threshold == 0.5
    assert settings.livekit_input_vad_force_cpu is True
    assert settings.livekit_max_turn_ms == 30000


def test_settings_reject_invalid_livekit_output_frame_ms() -> None:
    with pytest.raises(ValidationError, match="LIVEKIT_OUTPUT_FRAME_MS"):
        Settings(LIVEKIT_OUTPUT_FRAME_MS=30)


def test_settings_reject_livekit_output_queue_smaller_than_frame() -> None:
    with pytest.raises(ValidationError, match="LIVEKIT_OUTPUT_QUEUE_MS"):
        Settings(
            LIVEKIT_OUTPUT_FRAME_MS=20,
            LIVEKIT_OUTPUT_QUEUE_MS=10,
        )


def test_settings_reject_livekit_output_queue_above_barge_in_limit() -> None:
    with pytest.raises(ValidationError, match="LIVEKIT_OUTPUT_QUEUE_MS"):
        Settings(LIVEKIT_OUTPUT_QUEUE_MS=1000)


def test_settings_reject_livekit_max_turn_below_one_second() -> None:
    with pytest.raises(ValidationError, match="LIVEKIT_MAX_TURN_MS"):
        Settings(LIVEKIT_MAX_TURN_MS=999)


def test_settings_reject_livekit_max_turn_above_thirty_seconds() -> None:
    with pytest.raises(ValidationError, match="LIVEKIT_MAX_TURN_MS"):
        Settings(LIVEKIT_MAX_TURN_MS=30001)


def test_settings_reject_unsupported_livekit_qwen_input_chunk_ms() -> None:
    with pytest.raises(ValidationError, match="LIVEKIT_QWEN_INPUT_CHUNK_MS"):
        Settings(LIVEKIT_QWEN_INPUT_CHUNK_MS=20)


def test_settings_reject_non_positive_qwen_first_audio_timeout() -> None:
    with pytest.raises(ValidationError, match="QWEN_FIRST_AUDIO_TIMEOUT_SECONDS"):
        Settings(QWEN_FIRST_AUDIO_TIMEOUT_SECONDS=0)


def test_settings_reject_total_response_timeout_shorter_than_first_audio_timeout() -> None:
    with pytest.raises(ValidationError, match="QWEN_TOTAL_RESPONSE_TIMEOUT_SECONDS"):
        Settings(
            QWEN_FIRST_AUDIO_TIMEOUT_SECONDS=5,
            QWEN_TOTAL_RESPONSE_TIMEOUT_SECONDS=4,
        )


def test_settings_reject_non_positive_livekit_drain_timeout() -> None:
    with pytest.raises(ValidationError, match="LIVEKIT_DRAIN_TIMEOUT_SECONDS"):
        Settings(LIVEKIT_DRAIN_TIMEOUT_SECONDS=0)


def test_settings_reject_non_positive_livekit_browser_idle_timeout() -> None:
    with pytest.raises(ValidationError, match="LIVEKIT_BROWSER_IDLE_TIMEOUT_SECONDS"):
        Settings(LIVEKIT_BROWSER_IDLE_TIMEOUT_SECONDS=0)


def test_settings_reject_non_positive_qwen_prewarm_max_wait() -> None:
    with pytest.raises(ValidationError, match="QWEN_PREWARM_MAX_WAIT_SECONDS"):
        Settings(QWEN_PREWARM_MAX_WAIT_SECONDS=0)


def test_settings_reject_non_positive_qwen_prewarm_retry_interval() -> None:
    with pytest.raises(ValidationError, match="QWEN_PREWARM_RETRY_INTERVAL_SECONDS"):
        Settings(QWEN_PREWARM_RETRY_INTERVAL_SECONDS=0)
