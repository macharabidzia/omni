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
