# HotDocs Advance MCP server

[![CI](https://github.com/RosenAdvertising/hotdocs-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/RosenAdvertising/hotdocs-mcp/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-2026--07--28-7C3AED.svg)](https://modelcontextprotocol.io)

Connect Claude and other MCP clients to HotDocs Advance to work with work items, templates, answers and documents.

HotDocs Advance MCP server is a [Model Context Protocol](https://modelcontextprotocol.io) server for HotDocs Advance, a document automation platform. It registers 13 tools: 9 that read data and 4 that create work items and versions, update answers and complete versions, each requiring `confirm=true`. It runs over stdio by default, for desktop clients such as Claude Desktop, and offers an opt-in stateless Streamable HTTP mode that implements MCP specification 2026-07-28. HotDocs Advance credentials stay on the machine that runs the server: they come from environment variables, never from the client, and are never accepted as tool inputs. The server authenticates with the documented `ResourceOwner` API-client and service-principal flow.

## Features

- Read work items, work groups, template packages, variable data, answers, and documents.
- Create work items and versions, update answer sets, and complete versions. Every write tool
  requires explicit `confirm=true`.
- Require callers to attest that answer XML was validated against HotDocs' published Answer Set
  XSD before writing it.
- Return downloaded documents as structured JSON with base64-encoded content.
- Retry rate-limited and server-error responses to read requests with `Retry-After` support and
  exponential backoff; write requests are sent once and never retried. Failed calls return fixed error messages; HotDocs response text is never passed to the client.
- Make one request per collection and cap returned top-level arrays locally (default 50, maximum
  100). The public API documentation does not define pagination parameters or continuation fields,
  so the tools do not invent them.

## Tools

The server registers 13 tools.

| Tool | What it does |
| --- | --- |
| `list_work_items` | Find work items you are allowed to see. |
| `list_work_group_items` | List the work items held in one work group. |
| `list_template_packages` | List the templates available for creating work items. |
| `get_template_variable_data` | Show the questions and data fields used by a template version. |
| `get_work_item_answers` | Retrieve the saved answers for a work item, as XML or JSON. |
| `list_work_item_documents` | List documents from a work item's latest completed version. |
| `list_work_item_version_documents` | List documents from one historical work item version. |
| `get_work_item_document` | Show one document and any questions that were left unanswered. |
| `download_work_item_document` | Download an assembled document as base64-encoded JSON. |
| `create_work_item` | Create a new work item. Requires `confirm=true`. |
| `create_work_item_version` | Start a new assembly version. Requires `confirm=true`. |
| `patch_work_item_answers` | Overlay selected saved answers. Requires `confirm=true` and the XSD attestation. |
| `complete_work_item_version` | Finish assembly and produce documents. Requires `confirm=true`; an optional callback URL must pass the [callback destination policy](#callback-destination-policy). |

## Requirements

- Python 3.10+
- HotDocs Advance API client and service-principal credentials
- An MCP client such as Claude Desktop

## Installation

Install [uv](https://docs.astral.sh/uv/), then clone the repository and install its locked dependencies:

```bash
git clone https://github.com/RosenAdvertising/hotdocs-mcp.git
cd hotdocs-mcp
uv sync --locked
```

## Configuration

HotDocs Advance credentials come from environment variables. Set them in the environment that launches the server, such as the `env` object in your MCP client configuration.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `HOTDOCS_TENANCY_MONIKER` | Yes | None | Your HotDocs Advance tenancy moniker: letters, numbers and hyphens only. The server connects to `https://<moniker>.hotdocsadvance.com`. |
| `HOTDOCS_CLIENT_NAME` | Yes | None | API client name. |
| `HOTDOCS_PRINCIPAL_NAME` | Yes | None | Service-principal name. |
| `HOTDOCS_PRINCIPAL_PASSWORD` | Yes | None | Service-principal password. |
| `HOTDOCS_API_VERSION` | No | `2.0` | HotDocs Advance API version. Only `2.0` is accepted. |
| `HOTDOCS_ALLOWED_DESTINATION_HOSTS` | No | Empty | Comma-separated hosts allowed as callback destinations. While it is empty, any callback URL is refused. See [Callback destination policy](#callback-destination-policy). |

See [.env.example](.env.example) for placeholder values. The application reads environment variables directly and does not load `.env` files automatically. Keep real credentials out of source control.

The server starts and answers MCP initialization and tool listing without credentials. Tools report a configuration error until the required environment is provided. Two commands help with setup:

```bash
uv run hotdocs-mcp-setup
uv run hotdocs-mcp-verify
```

`hotdocs-mcp-setup` prints environment-variable instructions. `hotdocs-mcp-verify` validates the local configuration and exits with code 2 if it is missing or invalid; it does not contact HotDocs Advance or prove authentication. Neither command saves or prints secret values.

## Usage with Claude Desktop

Add the server to Claude Desktop's configuration file (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "hotdocs-advance": {
      "command": "uv",
      "args": ["run", "--locked", "--directory", "/absolute/path/to/hotdocs-mcp", "hotdocs-mcp"],
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

Replace `/absolute/path/to/hotdocs-mcp` with the path of your clone and the placeholder values with your credentials. Keep real credentials in your client's protected environment or secret store. Restart Claude Desktop after saving. Any other stdio MCP client uses the same command and arguments.

## HTTP mode

Stdio is the default. Set `HOTDOCS_MCP_TRANSPORT=streamable-http` to serve the stateless Streamable HTTP transport from MCP specification 2026-07-28 at `/mcp`. Each request stands alone: no initialization handshake and no `Mcp-Session-Id`. Clients on earlier protocol versions are served on the same endpoint.

> **Security: this endpoint has no authentication and no TLS.** Anyone who can reach the port can run every tool, including write tools, with this server's vendor credentials. Keep the default loopback bind (`127.0.0.1`), or put the server behind an authenticating TLS proxy on a private network. `HOTDOCS_MCP_ALLOWED_HOSTS` and `HOTDOCS_MCP_ALLOWED_ORIGINS` protect against browser DNS rebinding, not against direct callers. A proxy in front of it needs connection and idle timeouts: a legacy-style `GET /mcp` with `Accept: text/event-stream` holds a stream open until the client disconnects.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOTDOCS_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http`. |
| `HOTDOCS_MCP_HOST` | `127.0.0.1` | Bind address. `127.0.0.1`, `localhost` and `::1` use the SDK's built-in Host and Origin checks; any other value requires `HOTDOCS_MCP_ALLOWED_HOSTS`. |
| `PORT` | `8080` | Port; must be an integer. |
| `HOTDOCS_MCP_ALLOWED_HOSTS` | unset | Comma-separated `Host` header values accepted on a non-loopback bind, such as `mcp.example.com:8080` or `mcp.example.com:*`. |
| `HOTDOCS_MCP_ALLOWED_ORIGINS` | unset | Comma-separated `Origin` values accepted on a non-loopback bind, such as `https://client.example.com`. Requests without an `Origin` header are accepted; any other `Origin` is refused, including when this is unset. |

HotDocs Advance credentials come from the same configuration as stdio (see [Configuration](#configuration)), never from the request. Tool discovery works without vendor credentials; executing vendor tools requires them.

With the HotDocs Advance variables from Configuration exported in the process environment, run:

```bash
HOTDOCS_MCP_TRANSPORT=streamable-http PORT=8080 uv run --locked hotdocs-mcp
```

Point the MCP client at `http://127.0.0.1:8080/mcp`. Responses use the SDK's default SSE behavior, so a client that disconnects cancels its running request.

## Error handling

A failed tool call returns an MCP error result (`isError`) with a fixed message. HotDocs Advance's response text is never returned to the client and is not written to the log.

| Situation | The message begins |
| --- | --- |
| Missing or invalid configuration | `hotdocs is not configured or its configuration is invalid.` It names the four required variables and the `hotdocs-mcp-setup` command. |
| HTTP 401 | `Authentication failed.` |
| HTTP 403 | `Access denied.` |
| HTTP 404 | `Resource not found.` |
| HTTP 429 | `Rate limit reached.` |
| HTTP 5xx | `Service unavailable.` |
| Any other HTTP error | `Request rejected or response invalid.` |
| Network failure or timeout | `HotDocs request failed because the connection timed out or was unavailable.` |
| Unexpected response body | `Invalid tool input or response.` or `The operation could not be completed.` |

Input problems have their own messages: a write tool called without `confirm=true` returns `Confirmation required: set confirm=true only after reviewing the ... operation.`, Answer XML without `answer_xml_is_xsd_validated=true` is refused, a callback host that is not in `HOTDOCS_ALLOWED_DESTINATION_HOSTS` is refused, and a record ID that is not a plain identifier is rejected before any request is sent.

Read requests (`GET`) and the sign-in request that are answered with HTTP 429 or 5xx are retried up to 3 times. The four write tools (`create_work_item`, `create_work_item_version`, `patch_work_item_answers` and `complete_work_item_version`, which send `PUT` or `PATCH`) send their request once and never retry it, so one tool call cannot apply a change twice. The wait before a retry is the `Retry-After` value (seconds or an HTTP date) when present, and otherwise 0.25, 0.5 and 1 seconds. A 401 on an API call discards the cached token, signs in again and repeats the request once. Network failures are not retried. Requests time out after 30 seconds.

At startup the server exits with a message and status 1 on an unsupported `HOTDOCS_MCP_TRANSPORT`, a `PORT` that is not an integer, or a non-loopback `HOTDOCS_MCP_HOST` without `HOTDOCS_MCP_ALLOWED_HOSTS`. Missing credentials do not stop the server from starting; tool calls report the configuration error instead.

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

## Testing

The test suite runs offline and needs no HotDocs Advance account: every HotDocs Advance API call is intercepted at the HTTP layer with `respx`. It covers each tool's requests and responses, input validation, the callback destination policy, error handling, credential loading, pagination and retries, the stdio server, and the Streamable HTTP transport including the 2026-07-28 wire format, Host and Origin checks and stateless requests.

```bash
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
```

CI runs the suite on every push and pull request to `main`.

## License

MIT. See [LICENSE](LICENSE).
