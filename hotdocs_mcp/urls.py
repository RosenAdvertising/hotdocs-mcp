"""Offline URL boundary; DNS is resolved only by the downstream URL consumer."""

import ipaddress
import json
import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from mcp.server.mcpserver.exceptions import ToolError


def public_https(value: str) -> None:
    """Require unambiguous HTTPS and reject non-global numeric destinations."""
    try:
        if not isinstance(value, str) or not value.isascii():
            raise ValueError from None
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError from None
        parsed = urlsplit(value)
        _ = parsed.port
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "#" in value
            or "\\" in value
            or "%" in parsed.netloc
            or parsed.netloc.endswith(":")
        ):
            raise ValueError from None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Reject all legacy inet_aton/WHATWG numeric forms rather than letting
            # a downstream parser reinterpret hex, octal, decimal or short IPv4.
            labels = host.split(".")
            if (
                len(labels) < 2
                or host == "localhost"
                or host.endswith((".localhost", ".local", ".internal", ".home", ".lan"))
                or re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", labels[-1])
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels)
            ):
                raise ValueError from None
        else:
            mapped = getattr(address, "ipv4_mapped", None)
            if mapped is not None:
                address = mapped
            if (
                not address.is_global
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
                or address.is_loopback
                or address.is_link_local
            ):
                raise ValueError from None
    except (ValueError, UnicodeError):
        raise ToolError(
            "URL must be public HTTPS without userinfo or fragment; "
            "local, non-global and encoded numeric hosts are not allowed."
        ) from None


DESTINATION_SETTING = "HOTDOCS_ALLOWED_DESTINATION_HOSTS"
# Exact names after case/punctuation normalization, never substring matching.
URL_FIELDS = frozenset(
    {
        "url",
        "urls",
        "uri",
        "uris",
        "href",
        "callback",
        "endpoint",
        "callbackurl",
        "callbackuri",
        "targeturl",
        "targeturi",
        "redirecturl",
        "redirecturls",
        "urltofetch",
        "urllist",
        "baseurl",
        "baseurlpattern",
        "webhookurl",
        "uploadurl",
        "downloadurl",
        "storageurl",
        "destinationurl",
    }
)
# Only this explicitly designated JSON payload field permits decoding strings.
# Ordinary text (including description/name/answerXml) is always opaque.
JSON_PAYLOAD_FIELDS = frozenset({"payloadjson"})


def normalized_host(host: str) -> str:
    """Canonicalize administrator hostnames for exact, label-boundary comparisons."""
    host = host.rstrip(".").lower()
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host.encode("idna").decode("ascii").lower()


def destination_https(value: str) -> None:
    """Require both the literal URL policy and administrator destination approval."""
    public_https(value)
    raw = os.environ.get(DESTINATION_SETTING, "")
    try:
        entries = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            subtree = entry.startswith(".")
            host = normalized_host(entry[1:] if subtree else entry)
            # Entries are hosts, never URLs, ports, wildcards or userinfo.
            authority = f"[{host}]" if ":" in host else host
            public_https(f"https://{authority}/")
            if any(char in host for char in "/@?#%\\"):
                raise ValueError
            if subtree and ":" in host:
                raise ValueError
            entries.append((host, subtree))
        if not entries:
            raise ValueError
    except (ValueError, UnicodeError, ToolError):
        raise ToolError(
            f"Set {DESTINATION_SETTING} to comma-separated public destination hosts "
            "(exact host or .example.com for that domain and its subdomains)."
        ) from None
    host = normalized_host(urlsplit(value).hostname or "")
    if not any(
        host == allowed or (subtree and host.endswith("." + allowed))
        for allowed, subtree in entries
    ):
        raise ToolError(f"Destination host is not permitted by {DESTINATION_SETTING}.")


def validate_url_arguments(value: object, *, url_value: bool = False) -> None:
    """Inspect structures; decode strings only in declared JSON payload fields."""
    if isinstance(value, Mapping):
        if url_value:
            raise ToolError("URL fields must contain public HTTPS strings.")
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in JSON_PAYLOAD_FIELDS and isinstance(item, str):
                try:
                    item = json.loads(item)
                except ValueError:
                    continue
            validate_url_arguments(item, url_value=normalized in URL_FIELDS)
    elif isinstance(value, list | tuple):
        for item in value:
            validate_url_arguments(item, url_value=url_value)
    elif url_value and value is not None:
        if not isinstance(value, str):
            raise ToolError("URL fields must contain public HTTPS strings.")
        destination_https(value)
