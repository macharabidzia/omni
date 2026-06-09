from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_QUERY_KEY_FRAGMENTS = (
    "token",
    "secret",
    "signature",
    "sig",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "password",
)


def redact_url_secrets(url: str) -> str:
    if not url:
        return url

    try:
        parts = urlsplit(url)
    except Exception:
        return url

    if not parts.scheme and not parts.netloc:
        return url

    netloc = parts.netloc
    if "@" in netloc:
        _userinfo, host = netloc.rsplit("@", 1)
        netloc = f"***:***@{host}"

    if parts.query:
        query_pairs = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if any(fragment in key.lower() for fragment in _SENSITIVE_QUERY_KEY_FRAGMENTS):
                query_pairs.append((key, "***"))
            else:
                query_pairs.append((key, value))
        query = urlencode(query_pairs, doseq=True)
    else:
        query = parts.query

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
