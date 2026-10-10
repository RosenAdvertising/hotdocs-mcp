# HotDocs Advance MCP server

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/RosenAdvertising/hotdocs-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/RosenAdvertising/hotdocs-mcp/actions/workflows/ci.yml)

A Model Context Protocol (MCP) server that gives AI assistants a typed interface to 13
method-verified HotDocs Advance REST API operations. It uses stdio and the official MCP Python
SDK. The server authenticates with the documented `ResourceOwner` API-client and
service-principal flow. Credentials are provided through environment variables and are never
accepted as tool inputs.

## Features

- Read work items, work groups, template packages, variable data, answers, and documents.
- Create work items and versions, update answer sets, and complete versions. Every write tool
  requires explicit `confirm=true`.
- Require callers to attest that answer XML was validated against HotDocs' published Answer Set
  XSD before writing it.
- Return downloaded documents as structured JSON with base64-encoded content.
- Retry rate-limited and server-error responses with `Retry-After` support and exponential
  backoff. API errors preserve status, headers, content type, and response body.
- Make one request per collection and cap returned top-level arrays locally (default 50, maximum
  100). The public API documentation does not define pagination parameters or continuation fields,
  so the tools do not invent them.

## Requirements

- Python 3.12
- HotDocs Advance API client and service-principal credentials
- An MCP-compatible client

## Install from source

```bash
git clone https://github.com/RosenAdvertising/hotdocs-mcp.git
cd hotdocs-mcp
uv sync --locked
```

## Configure credentials

Set these variables in the environment used to launch the server:

| Variable | Purpose |
| --- | --- |
| `HOTDOCS_TENANCY_MONIKER` | Your HotDocs Advance tenancy moniker |
| `HOTDOCS_CLIENT_NAME` | API client name |
| `HOTDOCS_PRINCIPAL_NAME` | Service-principal name |
| `HOTDOCS_PRINCIPAL_PASSWORD` | Service-principal password |
| `HOTDOCS_API_VERSION` | API version; currently `2.0` (optional) |

See [.env.example](.env.example) for placeholder values. The application reads environment
variables directly and does not load `.env` files automatically. Keep real credentials out of
source control.

## MCP client configuration

Configure your MCP client to run the checkout directory. For example, a client that accepts
JSON configuration can use:

```json
{
  "mcpServers": {
    "hotdocs-advance": {
      "command": "uv",
      "args": ["--directory", "/path/to/hotdocs-mcp", "run", "hotdocs-mcp"],
      "env": {
        "HOTDOCS_TENANCY_MONIKER": "your-tenancy-moniker",
        "HOTDOCS_CLIENT_NAME": "your-api-client-name",
        "HOTDOCS_PRINCIPAL_NAME": "your-service-principal-name",
        "HOTDOCS_PRINCIPAL_PASSWORD": "set-in-your-local-client-config"
      }
    }
  }
}
```

Replace the directory and placeholder values with your local checkout and credentials. Protect
client configuration files that contain credentials.

## HTTP mode

Stdio is the default. To serve stateless Streamable HTTP, export the same HotDocs
environment variables listed above and select `streamable-http`. The endpoint is
`/mcp`; the SDK serves both current and legacy protocol clients on that path.
Responses use the SDK's default SSE behavior so disconnecting cancels a running
request. Vendor credentials come only from the server environment.

> **Security: this endpoint has no authentication and no TLS.** Anyone who can reach the port can run every tool, including write and delete tools, with this server's vendor credentials. Keep the default loopback bind (`127.0.0.1`), or put the server behind an authenticating TLS proxy on a private network. `HOTDOCS_MCP_ALLOWED_HOSTS` and `HOTDOCS_MCP_ALLOWED_ORIGINS` protect against browser DNS rebinding, not against direct callers. A proxy in front of it needs connection and idle timeouts: a legacy-style `GET /mcp` with `Accept: text/event-stream` holds a stream open until the client disconnects.

| Variable | Purpose / default |
| --- | --- |
| `HOTDOCS_MCP_TRANSPORT` | `stdio` (default) or `streamable-http` |
| `HOTDOCS_MCP_HOST` | Bind address; default `127.0.0.1` |
| `PORT` | Integer listening port; default `8080` |
| `HOTDOCS_MCP_ALLOWED_HOSTS` | Comma-separated allowed Host headers; required for non-loopback binds. Include ports, e.g. `mcp.example.com:8080`, or use `mcp.example.com:*` |
| `HOTDOCS_MCP_ALLOWED_ORIGINS` | Optional comma-separated allowed Origins, e.g. `https://client.example.com`; unlisted Origins are refused |
| `HOTDOCS_TENANCY_MONIKER` | HotDocs Advance tenancy moniker |
| `HOTDOCS_CLIENT_NAME` | API client name |
| `HOTDOCS_PRINCIPAL_NAME` | Service-principal name |
| `HOTDOCS_PRINCIPAL_PASSWORD` | Service-principal password |
| `HOTDOCS_API_VERSION` | Vendor API version; default `2.0` |
| `HOTDOCS_ALLOWED_DESTINATION_HOSTS` | Optional callback destination allowlist, as described below |

Loopback binds (`127.0.0.1`, `localhost`, `::1`) use the SDK's local Host and Origin
checks. Non-loopback binds require an explicit Host allowlist and validate every
supplied Origin against `HOTDOCS_MCP_ALLOWED_ORIGINS`; requests without an Origin
are permitted.

```bash
# After exporting the HotDocs credentials in this shell:
HOTDOCS_MCP_TRANSPORT=streamable-http HOTDOCS_MCP_HOST=127.0.0.1 PORT=8080 \
  uv run --locked hotdocs-mcp
```

Connect an HTTP-capable MCP client to `http://127.0.0.1:8080/mcp`. Tool discovery
works without vendor credentials; executing vendor tools requires them.

## Development

```bash
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
```

The test suite uses mocked HTTP responses and does not require a live HotDocs tenancy.

## License

MIT. See [LICENSE](LICENSE).

## Local setup and verification

The server starts and answers MCP initialization and tool listing without credentials.
Tools report a configuration error until the required environment is provided.
Run `hotdocs-mcp-setup` for environment-variable instructions, then restart your MCP
launcher with those variables exported. Run `hotdocs-mcp-verify` to validate local
configuration (exit 2 if missing or invalid). This command does not contact the
vendor or prove authentication. Neither command saves or prints secret values.

## Callback destination policy

Set `HOTDOCS_ALLOWED_DESTINATION_HOSTS` in the MCP launcher's environment, for example
`HOTDOCS_ALLOWED_DESTINATION_HOSTS=callbacks.example.com,.trusted.example.com`.
Entries match exact hosts; a leading dot includes that domain and its subdomains.
Comparison ignores case and trailing dots and normalizes IDNA. URL inputs must be
ASCII (use punycode for internationalized hosts), public HTTPS, and contain no
userinfo or fragments. Existing private/local/alternate-numeric IP checks still apply.
An unset/empty list refuses any supplied callback before a request; completion
without a callback still works. Tools cannot change this administrator setting.
The same checks cover exact normalized destination keys in dictionaries/lists and
explicit `payload_json` fields; ordinary text is never parsed as JSON.

The allowlist prevents a model from choosing arbitrary data recipients, private-IP
DNS aliases such as `127.0.0.1.sslip.io`, or unapproved public redirectors. Approve
only trusted hosts: this server does not resolve DNS or follow callbacks, and
HotDocs/network operators must enforce safe resolution and redirect behavior for
approved hosts as well.
