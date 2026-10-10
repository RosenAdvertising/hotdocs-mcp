"""Shared test fixtures."""

from __future__ import annotations

import pytest

from hotdocs_mcp.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        tenancy_moniker="example-tenant",
        client_name="api-client",
        principal_name="service-principal",
        principal_password="test-password",
        api_version="2.0",
    )
