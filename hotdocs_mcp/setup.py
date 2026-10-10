"""Environment-only setup instructions and offline configuration validation."""

import sys

from hotdocs_mcp.config import ConfigError, Settings
from hotdocs_mcp.runtime import CONFIGURATION_HELP


def setup() -> int:
    """Explain configuration without reading, prompting for, or persisting secrets."""
    print(CONFIGURATION_HELP)
    print(
        "Export these values in the environment used by your MCP launcher. "
        "See .env.example and README.md for optional settings. "
        "No dotenv file is loaded automatically."
    )
    return 0


def verify() -> int:
    """Validate local configuration only; this does not verify vendor authentication."""
    try:
        Settings.from_env()
    except ConfigError:
        print(CONFIGURATION_HELP, file=sys.stderr)
        return 2
    print("Local configuration is valid. Vendor authentication has not been tested.")
    return 0
