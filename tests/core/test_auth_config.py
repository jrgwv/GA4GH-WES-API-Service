"""Tests for fail-closed authentication configuration (F03)."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.wes_service.config import Settings
from src.wes_service.core.callback_auth import verify_callback_api_key

AUTH_ENV = ("AUTH_METHOD", "BASIC_AUTH_USERS", "ALLOW_NO_AUTH")


@pytest.fixture
def clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove auth-related env vars so Settings defaults are exercised."""
    for name in AUTH_ENV:
        monkeypatch.delenv(name, raising=False)


def test_default_auth_method_is_api_token(clean_auth_env: None) -> None:
    """Default authentication method must be api_token."""
    assert Settings(_env_file=None).auth_method == "api_token"


def test_basic_without_users_fails_startup(clean_auth_env: None) -> None:
    """AUTH_METHOD=basic with empty BASIC_AUTH_USERS must refuse to load."""
    with pytest.raises(ValidationError, match="BASIC_AUTH_USERS"):
        Settings(_env_file=None, auth_method="basic", basic_auth_users="")


def test_basic_without_users_from_env_fails_startup(
    clean_auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same check when configured through environment variables."""
    monkeypatch.setenv("AUTH_METHOD", "basic")
    with pytest.raises(ValidationError, match="BASIC_AUTH_USERS"):
        Settings(_env_file=None)


def test_basic_with_users_loads(clean_auth_env: None) -> None:
    """AUTH_METHOD=basic with users configured loads normally."""
    s = Settings(_env_file=None, auth_method="basic", basic_auth_users="alice:$2b$12$x")
    assert s.auth_method == "basic"


def test_none_without_opt_in_fails_startup(
    clean_auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUTH_METHOD=none without ALLOW_NO_AUTH must refuse to load."""
    monkeypatch.setenv("AUTH_METHOD", "none")
    with pytest.raises(ValidationError, match="ALLOW_NO_AUTH"):
        Settings(_env_file=None)


def test_none_with_opt_in_loads(
    clean_auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUTH_METHOD=none with ALLOW_NO_AUTH=1 loads."""
    monkeypatch.setenv("AUTH_METHOD", "none")
    monkeypatch.setenv("ALLOW_NO_AUTH", "1")
    s = Settings(_env_file=None)
    assert s.auth_method == "none"
    assert s.allow_no_auth is True


class TestCallbackApiKey:
    """Callback API key verification."""

    def _settings(self, mock_settings, key: str) -> None:
        mock_settings.return_value.enable_callback_endpoint = True
        mock_settings.return_value.INTERNAL_CALLBACK_API_KEY = key

    @pytest.mark.asyncio
    async def test_valid_key(self) -> None:
        """Matching key is accepted."""
        with patch("src.wes_service.core.callback_auth.get_settings") as ms:
            self._settings(ms, "s3cret-key")
            assert await verify_callback_api_key("s3cret-key") == "s3cret-key"

    @pytest.mark.asyncio
    async def test_invalid_key(self) -> None:
        """Non-matching key is rejected with 403."""
        with patch("src.wes_service.core.callback_auth.get_settings") as ms:
            self._settings(ms, "s3cret-key")
            with pytest.raises(HTTPException) as exc:
                await verify_callback_api_key("s3cret-kez")
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unconfigured_key_fails_closed(self) -> None:
        """Empty configured key never authenticates (500, not accept)."""
        with patch("src.wes_service.core.callback_auth.get_settings") as ms:
            self._settings(ms, "")
            with pytest.raises(HTTPException) as exc:
                await verify_callback_api_key("")
            assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_uses_constant_time_compare(self) -> None:
        """Comparison goes through hmac.compare_digest."""
        with patch("src.wes_service.core.callback_auth.get_settings") as ms, patch(
            "src.wes_service.core.callback_auth.hmac.compare_digest", return_value=True
        ) as cd:
            self._settings(ms, "s3cret-key")
            await verify_callback_api_key("anything")
            cd.assert_called_once()
