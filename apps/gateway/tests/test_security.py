from src.security import redact_url_secrets


def test_redact_url_secrets_masks_userinfo_and_sensitive_query_values() -> None:
    value = redact_url_secrets(
        "ws://user:pass@example.com/v1/realtime?token=abc123&foo=bar&api_key=secret"
    )

    assert value == "ws://***:***@example.com/v1/realtime?token=%2A%2A%2A&foo=bar&api_key=%2A%2A%2A"


def test_redact_url_secrets_leaves_safe_urls_unchanged() -> None:
    value = redact_url_secrets("http://example.com/health?mode=ready")

    assert value == "http://example.com/health?mode=ready"
