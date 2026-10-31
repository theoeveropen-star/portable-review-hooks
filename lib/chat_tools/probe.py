"""Bounded, metadata-only health checks for the approved HTTP MCP registry.

Only initialize, notifications/initialized and tools/list are sent. No provider
login, discovery, tool execution, redirects, proxy environment or persistent MCP
session is involved. Server descriptions, schemas and errors never leave here.
"""
from copy import deepcopy
from datetime import datetime, timezone
import http.client
import json
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlsplit

from .registry import ToolRegistry, ToolRegistryError, _url


TIMEOUT = 8.0
CACHE_TTL = 30.0
RESPONSE_LIMIT = 1024 * 1024
PAGE_LIMIT = 16
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class _Failure(Exception):
    def __init__(self, status="unavailable", *, kind=None, http_status=None):
        self.status = status
        self.kind = kind
        self.http_status = http_status
        super().__init__(status)


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _Failure()
            result[key] = value
        return result

    def nonfinite(_):
        raise _Failure()

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite)


def _result(message, request_id):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise _Failure()
    if message.get("id") != request_id or "method" in message:
        return None
    if "error" in message:
        raise _Failure(kind="rpc_error")
    if not isinstance(message.get("result"), dict):
        raise _Failure(kind="invalid_response")
    return message["result"]


class _Transport:
    response_limit = RESPONSE_LIMIT
    """Direct HTTP, with one wall-clock budget for the complete server probe."""

    def __init__(self, url, deadline):
        self.parts = urlsplit(_url(url))
        self.deadline = deadline
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.active_socket = None

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise _Failure(kind="cancelled" if self.cancelled.is_set() else "timeout")
        return remaining

    def _remember(self, connection):
        with self.lock:
            if self.cancelled.is_set():
                connection.close()
                raise _Failure()
            self.active_socket = connection

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            connection = self.active_socket
            self.active_socket = None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def _connect(self, address, timeout=None, source_address=None):
        # DNS can block independently of socket timeouts. The outer daemon
        # worker is deadline-bounded; check again after DNS so a late resolver
        # cannot initiate a connection after the probe has been cancelled.
        for family, kind, protocol, _, sockaddr in socket.getaddrinfo(
                address[0], address[1], 0, socket.SOCK_STREAM):
            self.remaining()
            connection = socket.socket(family, kind, protocol)
            self._remember(connection)
            try:
                connection.settimeout(self.remaining())
                connection.connect(sockaddr)
                return connection
            except TimeoutError:
                connection.close()
                raise _Failure(kind="timeout") from None
            except OSError:
                connection.close()
        raise _Failure(kind="connection")

    def _chunks(self, response, connection):
        length = response.getheader("Content-Length")
        if length is not None and not length.isdigit():
            raise _Failure(kind="invalid_response")
        if length is not None and int(length) > self.response_limit:
            raise _Failure(kind="response_too_large")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise _Failure(kind="unsupported_encoding")
        consumed = 0
        while True:
            remaining = self.remaining()
            # read1 closes HTTPResponse's file when Content-Length reaches
            # zero. With Connection: close that also closes the last socket
            # reference: do not settimeout on that completed response (EBADF).
            if response.isclosed():
                return
            connection.settimeout(remaining)
            chunk = response.read1(min(65536, self.response_limit + 1 - consumed))
            self.remaining()
            if not chunk:
                return
            consumed += len(chunk)
            if consumed > self.response_limit:
                raise _Failure(kind="response_too_large")
            yield chunk

    def _response(self, response, connection, request_id):
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        chunks = self._chunks(response, connection)
        if content_type == "application/json":
            result = _result(_json(b"".join(chunks)), request_id)
            if result is None:
                raise _Failure()
            return result
        if content_type != "text/event-stream":
            raise _Failure(kind="unsupported_content_type")

        # SSE data is UTF-8 JSON. Stop at the matching response instead of
        # waiting for a persistent stream to close. Ignore unrelated events;
        # never act on any server-initiated request or metadata instruction.
        pending, data = bytearray(), []
        initial, skip_lf = True, False
        for chunk in chunks:
            if skip_lf:
                chunk = chunk[1:] if chunk.startswith(b"\n") else chunk
                skip_lf = False
            pending.extend(chunk)
            if initial:
                if len(pending) < 3 and b"\xef\xbb\xbf".startswith(pending):
                    continue
                if pending.startswith(b"\xef\xbb\xbf"):
                    del pending[:3]
                initial = False
            while pending:
                ends = [index for index in (pending.find(b"\n"), pending.find(b"\r")) if index >= 0]
                if not ends:
                    break
                end = min(ends)
                skip_lf = pending[end] == 13 and end + 1 == len(pending)
                width = 2 if pending[end:end + 2] == b"\r\n" else 1
                line = bytes(pending[:end])
                del pending[:end + width]
                if not line:
                    if data:
                        result = _result(_json(b"\n".join(data)), request_id)
                        data = []
                        if result is not None:
                            return result
                elif line.startswith(b"data:"):
                    value = line[5:]
                    data.append(value[1:] if value.startswith(b" ") else value)
                elif line == b"data":
                    data.append(b"")
        raise _Failure(kind="incomplete_stream")

    def post(self, message, headers, *, notification=False):
        parts = self.parts
        factory = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        options = {"timeout": self.remaining()}
        if parts.scheme == "https":
            options["context"] = ssl.create_default_context()
        client = factory(parts.hostname, parts.port, **options)
        # HTTPConnection does not inspect proxy variables or follow redirects.
        # Its injectable connector additionally enforces our shared deadline.
        client._create_connection = self._connect
        response = None
        try:
            client.connect()
            connection = client.sock
            self._remember(connection)
            connection.settimeout(self.remaining())
            path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
            client.request("POST", path, json.dumps(message, separators=(",", ":")).encode("utf-8"), {
                "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "Accept-Encoding": "identity", "Connection": "close", **headers})
            connection.settimeout(self.remaining())
            response = client.getresponse()
            self.remaining()
            if response.status in (401, 403):
                raise _Failure("auth_required", kind="unauthorized", http_status=response.status)
            if notification:
                if response.status not in (200, 202, 204):
                    raise _Failure()
                raw = b"".join(self._chunks(response, connection))
                if raw:
                    # A few explicitly registered home services predate the
                    # Streamable HTTP notification 202 response. Accept only
                    # their empty JSON-RPC acknowledgement, never content or
                    # an error disguised as successful initialization.
                    if response.status != 200 or response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise _Failure()
                    acknowledgement = _json(raw)
                    if (not isinstance(acknowledgement, dict)
                            or set(acknowledgement) != {"jsonrpc", "id", "result"}
                            or acknowledgement["jsonrpc"] != "2.0"
                            or acknowledgement["id"] not in (None, 0)
                            or isinstance(acknowledgement["id"], bool)
                            or acknowledgement["result"] != {}):
                        raise _Failure()
                return None, None
            if response.status != 200:
                raise _Failure(kind="redirect_refused" if 300 <= response.status < 400 else "http_error", http_status=response.status)
            session = response.getheader("Mcp-Session-Id")
            if session is not None and (not session or len(session) > 4096
                                       or any(ord(char) < 33 or ord(char) > 126 for char in session)):
                raise _Failure()
            return self._response(response, connection, message["id"]), session
        finally:
            if response is not None:
                response.close()
            client.close()
            with self.lock:
                self.active_socket = None


class _Probe:
    def __init__(self, server, deadline, registry=None):
        self.server = server
        self.registry = registry
        self.deadline = deadline
        self.transport = None
        self.value = None

    def receipt(self, status, available=()):
        missing = [name for name in self.server.tools if name not in available]
        return {"name": self.server.name, "label": self.server.label, "status": status,
                "expected_count": len(self.server.tools), "available_count": len(self.server.tools) - len(missing),
                "missing_tools": missing,
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}

    def run(self):
        try:
            settings = (self.registry.native_settings(self.server) if self.registry is not None else
                        {"url": self.server.url, "bearer_token_env_var":
                         self.server.token_variable if self.server.token_file is not None else None})
            routed = self.registry is not None and self.registry._relay_url is not None
            self.transport = _Transport(settings["url"], self.deadline)
            try:
                environment = self.registry.environment() if routed else ToolRegistry((self.server,)).environment()
            except ToolRegistryError:
                raise _Failure("auth_required") from None
            headers = {}
            if settings["bearer_token_env_var"] is not None:
                headers["Authorization"] = "Bearer " + environment[settings["bearer_token_env_var"]]
            initialized, session = self.transport.post({
                "jsonrpc": "2.0", "id": "probe-init", "method": "initialize", "params": {
                    "protocolVersion": PROTOCOL_VERSIONS[0], "capabilities": {},
                    "clientInfo": {"name": "companion-chat-tools-probe", "version": "1"}}}, headers)
            version = initialized.get("protocolVersion")
            capabilities = initialized.get("capabilities")
            if version not in PROTOCOL_VERSIONS or not isinstance(capabilities, dict):
                raise _Failure()
            headers["MCP-Protocol-Version"] = version
            if session is not None:
                headers["Mcp-Session-Id"] = session
            self.transport.post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                                headers, notification=True)
            if not isinstance(capabilities.get("tools"), dict):
                self.value = self.receipt("missing_tools")
                return
            available, cursors, cursor = set(), set(), None
            expected = set(self.server.tools)
            for page in range(PAGE_LIMIT):
                message = {"jsonrpc": "2.0", "id": "probe-tools-" + str(page), "method": "tools/list",
                           "params": {} if cursor is None else {"cursor": cursor}}
                result, _ = self.transport.post(message, headers)
                tools = result.get("tools")
                if not isinstance(tools, list):
                    raise _Failure()
                for tool in tools:
                    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
                        raise _Failure()
                    if tool["name"] in expected:
                        if routed:
                            from .descriptions import tool_note
                            note = tool_note(self.server.name, tool["name"])
                            description = tool.get("description", "")
                            if note and (not isinstance(description, str) or not
                                         (description == note or description.endswith("\n\n" + note))):
                                raise _Failure("description_missing")
                        available.add(tool["name"])
                self.transport.remaining()
                if available == expected:
                    self.value = self.receipt("ready", available)
                    return
                cursor = result.get("nextCursor")
                if cursor is None:
                    self.value = self.receipt("missing_tools", available)
                    return
                if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in cursors:
                    raise _Failure()
                cursors.add(cursor)
            raise _Failure()
        except _Failure as error:
            self.value = self.receipt(error.status)
        except Exception:
            # Never surface network exception text, token paths, remote errors,
            # tool descriptions, schema fragments or server instructions.
            self.value = self.receipt("unavailable")


def probe(registry):
    """Return a safe receipt per registered server, in registry order.

    ``available_count`` counts only explicitly expected/allowed tools. A ready
    receipt proves metadata connectivity, not that a business tool was called.
    No implicit stateless fallback or legacy SSE endpoint discovery is allowed.
    """
    deadline = time.monotonic() + TIMEOUT
    jobs = [_Probe(server, deadline, registry) for server in registry._servers]
    workers = [threading.Thread(target=job.run, daemon=True, name="chat-tool-probe") for job in jobs]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(max(0, deadline - time.monotonic()))
    result = []
    for job, worker in zip(jobs, workers):
        if worker.is_alive():
            if job.transport is not None:
                job.transport.cancel()
            result.append(job.receipt("unavailable"))
        else:
            result.append(job.value or job.receipt("unavailable"))
    return result


class ToolProbe:
    """Thirty-second, single-flight cache; force refreshes never fan out."""

    def __init__(self, registry):
        self.registry = registry
        self._condition = threading.Condition()
        self._running = False
        self._value = None
        self._expires = 0.0
        self._generation = 0

    def status(self, force=False):
        with self._condition:
            generation = self._generation
            while self._running:
                self._condition.wait()
            # A concurrent forced caller can share the refresh it waited for.
            if self._value is not None and (self._generation != generation or
                                           (not force and time.monotonic() < self._expires)):
                return deepcopy(self._value)
            self._running = True
        try:
            value = probe(self.registry)
        finally:
            with self._condition:
                if "value" in locals():
                    self._value = value
                    self._expires = time.monotonic() + CACHE_TTL
                    self._generation += 1
                self._running = False
                self._condition.notify_all()
        return deepcopy(value)
