"""Runtime configuration (environment variables / .env file)."""

from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fragments that mark a token copied from .env.example rather than generated.
_PLACEHOLDER_TOKEN_MARKERS = ("change-me", "changeme", "change_me")


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Nextcloud -------------------------------------------------------
    nextcloud_url: str = Field(description="Base URL of the Nextcloud instance")
    nextcloud_user: str = Field(description="Nextcloud user id used in the DAV files path")
    nextcloud_login: str | None = Field(default=None, description="Login name if different")
    nextcloud_password: SecretStr = Field(description="Nextcloud app password")
    nextcloud_sync_folder: str = Field(default="/super-productivity")
    nextcloud_allow_http: bool = Field(
        default=False,
        description="Allow a plain http:// NEXTCLOUD_URL for a host other than localhost",
    )

    # --- Super Productivity sync file -------------------------------------
    sp_encryption_password: SecretStr | None = None
    sp_allow_plaintext: bool = Field(
        default=False,
        description="Accept a plaintext sync file even though an encryption password is set",
    )
    sp_timezone: str = "UTC"
    sp_client_id: str | None = None
    sp_cache_ttl_seconds: float = 10.0
    sp_verify_upload: bool = Field(
        default=False,
        description="Re-download after each upload and compare hashes instead of trusting the PUT etag",
    )
    sp_write_backup: bool = True
    sp_backup_retention_days: int = Field(
        default=7, ge=1, le=365, description="Days to keep timestamped sync-data.json.*.bak files"
    )
    sp_max_write_attempts: int = 3
    http_timeout_seconds: float = 120.0
    http_max_retries: int = Field(default=2, ge=0, le=10, description="Retries for transient WebDAV failures")

    # --- MCP server -------------------------------------------------------
    mcp_auth_tokens: str = ""
    mcp_auth_token: str = ""  # single-token alias
    mcp_auth_disabled: bool = False
    mcp_allow_token_in_path: bool = False
    mcp_allowed_hosts: str = ""
    mcp_path: str = "/mcp"

    data_dir: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    @field_validator("nextcloud_url")
    @classmethod
    def _strip_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("NEXTCLOUD_URL must start with http:// or https://")
        return v

    @field_validator("sp_timezone")
    @classmethod
    def _check_timezone(cls, v: str) -> str:
        v = v.strip()
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"SP_TIMEZONE {v!r} is not a known IANA time zone (e.g. Europe/Berlin)") from e
        return v

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, v: str) -> str:
        v = v.strip().upper()
        if not isinstance(logging.getLevelNamesMapping().get(v), int):
            raise ValueError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return v

    @field_validator("sp_max_write_attempts")
    @classmethod
    def _check_attempts(cls, v: int) -> int:
        if v < 1:
            raise ValueError("SP_MAX_WRITE_ATTEMPTS must be at least 1")
        return v

    @property
    def auth_tokens(self) -> list[str]:
        raw = ",".join([self.mcp_auth_tokens, self.mcp_auth_token])
        return [t.strip() for t in raw.split(",") if t.strip()]

    @property
    def allowed_hosts(self) -> list[str]:
        return [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]

    @property
    def dav_login(self) -> str:
        return (self.nextcloud_login or "").strip() or self.nextcloud_user.strip()

    def validate_runtime(self) -> None:
        if not self.auth_tokens and not self.mcp_auth_disabled:
            raise ValueError(
                "No MCP_AUTH_TOKENS configured. Set at least one token or set "
                "MCP_AUTH_DISABLED=true if authentication is handled by the reverse proxy."
            )
        for t in self.auth_tokens:
            if len(t) < 16:
                raise ValueError("Every MCP auth token must be at least 16 characters long")
            if any(marker in t.lower() for marker in _PLACEHOLDER_TOKEN_MARKERS):
                raise ValueError(
                    "MCP_AUTH_TOKENS still contains the placeholder from .env.example; "
                    "generate a token with `openssl rand -hex 32`"
                )
        url = urlparse(self.nextcloud_url)
        if url.scheme == "http" and not self.nextcloud_allow_http and not _is_loopback(url.hostname):
            raise ValueError(
                "NEXTCLOUD_URL uses plain http://, which sends the Nextcloud app password and the "
                "whole sync file in clear text. Use https://, or set NEXTCLOUD_ALLOW_HTTP=true "
                "for a network you trust."
            )
