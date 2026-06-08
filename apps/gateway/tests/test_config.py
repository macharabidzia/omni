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


def test_settings_default_livekit_output_lowpass_hz() -> None:
    settings = Settings()

    assert settings.livekit_output_lowpass_hz == 6000.0
