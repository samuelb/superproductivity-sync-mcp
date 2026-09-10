import pytest
from pydantic import ValidationError

from superproductivity_sync_mcp.config import Settings

BASE = dict(
    nextcloud_url="https://cloud.example.com",
    nextcloud_user="alice",
    nextcloud_password="pw",
    mcp_auth_tokens="secret-token-1234567",
    _env_file=None,
)


def test_defaults_and_normalisation():
    s = Settings(
        **{**BASE, "nextcloud_url": "https://c.example.com/"}, sp_timezone=" Europe/Berlin ", log_level="debug"
    )
    assert s.sp_timezone == "Europe/Berlin" and s.log_level == "DEBUG"
    assert s.nextcloud_url == "https://c.example.com" and s.dav_login == "alice"
    s.validate_runtime()


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("sp_timezone", "Mars/Olympus", "SP_TIMEZONE"),
        ("log_level", "LOUD", "LOG_LEVEL"),
        ("sp_max_write_attempts", 0, "SP_MAX_WRITE_ATTEMPTS"),
        ("nextcloud_url", "cloud.example.com", "NEXTCLOUD_URL"),
    ],
)
def test_invalid_values_are_rejected_with_a_named_variable(field, value, fragment):
    with pytest.raises(ValidationError) as ei:
        Settings(**{**BASE, field: value})
    assert fragment in str(ei.value)


def test_runtime_validation_of_tokens():
    with pytest.raises(ValueError, match="No MCP_AUTH_TOKENS"):
        Settings(**{**BASE, "mcp_auth_tokens": ""}).validate_runtime()
    with pytest.raises(ValueError, match="at least 16"):
        Settings(**{**BASE, "mcp_auth_tokens": "short"}).validate_runtime()
    Settings(**{**BASE, "mcp_auth_tokens": "", "mcp_auth_disabled": True}).validate_runtime()
    s = Settings(
        **{**BASE, "mcp_auth_tokens": "aaaaaaaaaaaaaaaaaa, bbbbbbbbbbbbbbbbbb", "mcp_auth_token": "cccccccccccccccc"}
    )
    assert s.auth_tokens == ["aaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbb", "cccccccccccccccc"]


def test_placeholder_token_is_refused():
    for token in ("change-me-to-a-long-random-secret", "ChangeMe-and-then-some-more", "please_change_me_now"):
        with pytest.raises(ValueError, match="placeholder"):
            Settings(**{**BASE, "mcp_auth_tokens": token}).validate_runtime()


def test_env_example_never_starts_as_is():
    """The shipped example must fail validate_runtime, or a copy-paste deploy ships a public token."""
    import re
    from pathlib import Path

    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", Path(".env.example").read_text(), re.M))
    kwargs = {k.lower(): v for k, v in values.items() if v}
    with pytest.raises(ValueError, match="placeholder"):
        Settings(**kwargs, _env_file=None).validate_runtime()


@pytest.mark.parametrize(
    ("url", "allow", "ok"),
    [
        ("http://cloud.example.com", False, False),
        ("http://10.0.0.5:8080", False, False),
        ("http://cloud.example.com", True, True),
        ("http://localhost:8080", False, True),
        ("http://127.0.0.1", False, True),
        ("http://[::1]:8080", False, True),
        ("https://cloud.example.com", False, True),
    ],
)
def test_plain_http_needs_loopback_or_opt_in(url, allow, ok):
    s = Settings(**{**BASE, "nextcloud_url": url, "nextcloud_allow_http": allow})
    if ok:
        s.validate_runtime()
    else:
        with pytest.raises(ValueError, match="NEXTCLOUD_ALLOW_HTTP"):
            s.validate_runtime()
