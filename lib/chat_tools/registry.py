"""Load an approved MCP manifest, without discovery or provider authentication.

Registering a tool does not grant authority for every possible use. This module
only translates an explicit operator-reviewed allowlist; turn-level safeguards
remain the responsibility of the host and the connected tools.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit


VERSION = "companion-chat-tools/v1"
MANIFEST_LIMIT = 64 * 1024
TOKEN_LIMIT = 4096
SERVER_LIMIT = 18
TOOL_LIMIT = 287
QUALIFIED_TOOL_NAME_LIMIT = 96
_SERVER_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")


class ToolRegistryError(ValueError):
    """Fixed diagnostic codes that never include credentials or file content."""


def _fail(code="chat_tools_manifest_invalid"):
    raise ToolRegistryError(code)


def _text(value, maximum, *, whitespace=False):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or not value.strip()
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
            or (not whitespace and any(char.isspace() for char in value))):
        _fail()
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail()
    return value


def _url(value):
    value = _text(value, 4096)
    try:
        parts = urlsplit(value)
        port = parts.port
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or "?" in value or "#" in value
                or "\\" in value
                or (port is not None and not 1 <= port <= 65535)
                or (parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"))):
            _fail()
    except (TypeError, ValueError):
        _fail()
    return value


def _read(path, limit, code):
    """A bounded regular-file read; never block on a FIFO or read a device."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                _fail(code)
            raw = source.read(limit + 1)
        if not raw or len(raw) > limit:
            _fail(code)
        return raw
    except (OSError, ValueError):
        raise ToolRegistryError(code) from None


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: _fail())
    except (ValueError, UnicodeError, RecursionError):
        raise ToolRegistryError("chat_tools_manifest_invalid") from None


@dataclass(frozen=True)
class _Server:
    name: str
    label: str
    url: str = field(repr=False)
    token_file: str | None = field(repr=False)
    tools: tuple[str, ...]

    @property
    def token_variable(self):
        return "COMPANION_CHAT_MCP_" + self.name.upper() + "_TOKEN"


class ToolRegistry:
    def __init__(self, servers, *, relay_url=None, relay_token=None):
        self._servers = tuple(servers)
        self._relay_url = relay_url
        self._relay_token = relay_token
        if (relay_url is None) != (relay_token is None):
            _fail("chat_tools_relay_invalid")
        if relay_url is not None:
            parts = urlsplit(_url(relay_url))
            if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port
                    or parts.path or not isinstance(relay_token, str) or not 16 <= len(relay_token) <= TOKEN_LIMIT
                    or any(ord(char) < 33 or ord(char) > 126 for char in relay_token)):
                _fail("chat_tools_relay_invalid")
        metadata = [{"name": server.name, "label": server.label, "url": server.url,
                     "authenticated": server.token_file is not None, "tools": list(server.tools)}
                    for server in self._servers]
        self.sha256 = hashlib.sha256(json.dumps(
            {"version": VERSION, "servers": metadata}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.tools = tuple("mcp__" + server.name + "__" + re.sub(r"[^A-Za-z0-9_-]", "_", tool)
                           for server in self._servers for tool in server.tools)
        if len(set(self.tools)) != len(self.tools):
            _fail("chat_tools_normalized_name_collision")
        self.token_files = tuple(dict.fromkeys(server.token_file for server in self._servers
                                              if server.token_file is not None))

    @classmethod
    def from_file(cls, path):
        try:
            source = Path(path).resolve(strict=True)
        except (TypeError, ValueError, OSError, RuntimeError):
            raise ToolRegistryError("chat_tools_manifest_unavailable") from None
        value = _json(_read(source, MANIFEST_LIMIT, "chat_tools_manifest_unavailable"))
        if (not isinstance(value, dict) or set(value) != {"version", "servers"}
                or value.get("version") != VERSION or not isinstance(value.get("servers"), list)
                or len(value["servers"]) > SERVER_LIMIT):
            _fail()
        servers, names, count = [], set(), 0
        for item in value["servers"]:
            required = {"name", "label", "url", "tools"}
            if (not isinstance(item, dict) or not required <= set(item)
                    or set(item) - required - {"token_file"}):
                _fail()
            name = _text(item["name"], 32)
            if not _SERVER_NAME.fullmatch(name) or name in names:
                _fail()
            names.add(name)
            label = _text(item["label"], 128, whitespace=True)
            url = _url(item["url"])
            tools = item["tools"]
            if (not isinstance(tools, list) or not tools
                    or any(not isinstance(tool, str) or not _TOOL_NAME.fullmatch(tool) for tool in tools)
                    or len(set(tools)) != len(tools)):
                _fail()
            # The adapter guard and public activity wire schema share this
            # bound. Reject unsupported names at startup, not on first use.
            if any(len("mcp__" + name + "__" + tool) > QUALIFIED_TOOL_NAME_LIMIT for tool in tools):
                _fail()
            count += len(tools)
            if count > TOOL_LIMIT:
                _fail()
            token_file = None
            if "token_file" in item:
                raw_path = _text(item["token_file"], 4096, whitespace=True)
                try:
                    candidate = Path(raw_path)
                    token_file = str((candidate if candidate.is_absolute() else source.parent / candidate).resolve())
                except (OSError, ValueError, RuntimeError):
                    _fail()
            servers.append(_Server(name, label, url, token_file, tuple(sorted(tools))))
        return cls(sorted(servers, key=lambda server: server.name))

    def with_relay(self, url, token):
        """Route native MCP clients through the adapter's description layer.

        The approved upstream manifest and scope digest stay unchanged. The
        short-lived relay secret never enters config JSON, receipts or argv.
        """
        return type(self)(self._servers, relay_url=url, relay_token=token)

    def native_settings(self, server):
        return {"url": self._relay_url + "/mcp/" + server.name if self._relay_url else server.url,
                "bearer_token_env_var": server.token_variable
                if self._relay_url or server.token_file is not None else None}

    def upstream_config(self):
        """Stable logical configuration, including for existing Claude pins."""
        return type(self)(self._servers).config()

    def config(self):
        servers = {}
        for server in self._servers:
            settings = self.native_settings(server)
            entry = {"type": "http", "url": settings["url"]}
            if settings["bearer_token_env_var"] is not None:
                entry["headers"] = {"Authorization": "Bearer ${" + settings["bearer_token_env_var"] + "}"}
            servers[server.name] = entry
        return {"mcpServers": servers}

    def environment(self):
        if self._relay_url is not None:
            # Native model processes only need the narrower local relay token;
            # upstream service credentials stay in the adapter process.
            return {server.token_variable: self._relay_token for server in self._servers}
        environment = {}
        for server in self._servers:
            if server.token_file is None:
                continue
            raw = _read(server.token_file, TOKEN_LIMIT, "chat_tools_credential_unavailable")
            try:
                token = raw.decode("ascii").strip()
            except UnicodeError:
                raise ToolRegistryError("chat_tools_credential_invalid") from None
            if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
                _fail("chat_tools_credential_invalid")
            environment[server.token_variable] = token
        return environment

    def codex_overrides(self):
        overrides = []
        for server in self._servers:
            settings = self.native_settings(server)
            prefix = "mcp_servers." + server.name + "."
            overrides.extend([prefix + "enabled=true", prefix + "url=" + json.dumps(settings["url"], ensure_ascii=False),
                              prefix + "enabled_tools=" + json.dumps(list(server.tools), separators=(",", ":"))])
            if settings["bearer_token_env_var"] is not None:
                overrides.append(prefix + "bearer_token_env_var=" + json.dumps(settings["bearer_token_env_var"]))
        return overrides

    def receipt(self):
        return {"version": VERSION, "sha256": self.sha256, "server_count": len(self._servers),
                "tool_count": len(self.tools),
                "servers": [{"name": server.name, "label": server.label, "tools": list(server.tools),
                             "tool_count": len(server.tools)} for server in self._servers]}
