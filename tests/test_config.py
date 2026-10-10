"""Configuration and clean-startup tests."""

from __future__ import annotations

import pytest

from hotdocs_mcp.config import ConfigError, Settings


def test_missing_credentials_names_every_environment_variable() -> None:
    try:
        Settings.from_env({})
    except ConfigError as exc:
        message = str(exc)
    else:
        raise AssertionError("Settings.from_env should reject missing credentials")

    assert "HOTDOCS_TENANCY_MONIKER" in message
    assert "HOTDOCS_CLIENT_NAME" in message
    assert "HOTDOCS_PRINCIPAL_NAME" in message
    assert "HOTDOCS_PRINCIPAL_PASSWORD" in message


def test_startup_without_credentials_runs_stdio(monkeypatch, capsys) -> None:
    from hotdocs_mcp import server
    from hotdocs_mcp.runtime import MCPServer

    seen = []
    monkeypatch.setattr(MCPServer, "run", lambda self, **kwargs: seen.append(kwargs))
    server.main()
    assert seen == [{"transport": "stdio"}]
    assert "Traceback" not in capsys.readouterr().err


def test_settings_repr_does_not_expose_principal_password() -> None:
    settings = Settings("tenant", "client", "principal", "super-secret")
    assert "super-secret" not in repr(settings)


def test_only_method_verified_api_version_is_accepted() -> None:
    values = {
        "HOTDOCS_TENANCY_MONIKER": "tenant",
        "HOTDOCS_CLIENT_NAME": "client",
        "HOTDOCS_PRINCIPAL_NAME": "principal",
        "HOTDOCS_PRINCIPAL_PASSWORD": "password",
        "HOTDOCS_API_VERSION": "1.1",
    }
    with pytest.raises(ConfigError, match=r"supports HotDocs API v2\.0 only"):
        Settings.from_env(values)
