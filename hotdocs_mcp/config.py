"""Environment-only configuration for HotDocs Advance."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigError(ValueError):
    """Raised when startup configuration is absent or invalid."""


_REQUIRED_ENV_VARS = (
    "HOTDOCS_TENANCY_MONIKER",
    "HOTDOCS_CLIENT_NAME",
    "HOTDOCS_PRINCIPAL_NAME",
    "HOTDOCS_PRINCIPAL_PASSWORD",
)
_MONIKER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_VERSION_PATTERN = re.compile(r"^\d+\.\d+$")


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings loaded exclusively from environment variables."""

    tenancy_moniker: str
    client_name: str
    principal_name: str
    principal_password: str = field(repr=False)
    api_version: str = "2.0"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Load settings and name every missing credential in one actionable error."""
        values = os.environ if environ is None else environ
        missing = [name for name in _REQUIRED_ENV_VARS if not values.get(name, "").strip()]
        if missing:
            joined = ", ".join(missing)
            raise ConfigError(
                f"Missing required environment variable(s): {joined}. "
                "Copy .env.example and set the ResourceOwner credentials."
            )

        moniker = values["HOTDOCS_TENANCY_MONIKER"].strip()
        if not _MONIKER_PATTERN.fullmatch(moniker):
            raise ConfigError(
                "HOTDOCS_TENANCY_MONIKER must contain only letters, numbers, and hyphens."
            )

        api_version = values.get("HOTDOCS_API_VERSION", "2.0").strip()
        if not _VERSION_PATTERN.fullmatch(api_version):
            raise ConfigError("HOTDOCS_API_VERSION must look like '2.0'.")
        if api_version != "2.0":
            raise ConfigError(
                "HOTDOCS_API_VERSION must be '2.0'; this build is method-verified only for v2.0."
            )

        return cls(
            tenancy_moniker=moniker,
            client_name=values["HOTDOCS_CLIENT_NAME"].strip(),
            principal_name=values["HOTDOCS_PRINCIPAL_NAME"].strip(),
            principal_password=values["HOTDOCS_PRINCIPAL_PASSWORD"],
            api_version=api_version,
        )

    @property
    def tenancy_url(self) -> str:
        """Return the cloud tenancy origin documented for ResourceOwner auth."""
        return f"https://{self.tenancy_moniker}.hotdocsadvance.com"

    @property
    def auth_url(self) -> str:
        """Return the service-principal login endpoint."""
        return f"{self.tenancy_url}/auth/Authorize/ServicePrincipalLogIn"

    @property
    def api_base_url(self) -> str:
        """Return the versioned REST API base URL."""
        return f"{self.tenancy_url}/api/rest/v{self.api_version}/"
