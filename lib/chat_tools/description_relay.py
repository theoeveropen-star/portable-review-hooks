"""Bounded host-only HTTP transport; no model MCP routes."""
import hmac

import http.client

import json

import ssl

import threading

import time

from urllib.parse import quote

from .probe import _Failure, _Transport, _json

RESPONSE_LIMIT = 4 * 1024 * 1024

_FORWARDED_HEADERS = (("Mcp-Session-Id", 4096), ("MCP-Protocol-Version", 128))

class RelayError(ValueError):
    def __init__(self, code, status=400, rpc_code=-32602):
        super().__init__(code)
        self.code, self.status, self.rpc_code = code, status, rpc_code

def _header(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(33 <= ord(char) <= 126 for char in value))

def _envelope(value, identifier):
    if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
            or "id" not in value
            or type(value.get("id")) is not type(identifier) or value.get("id") != identifier
            or "method" in value or ("result" in value) == ("error" in value)):
        raise RelayError("chat_tool_upstream_invalid_response", 502, -32001)
    if "error" in value and (not isinstance(value["error"], dict)
                             or type(value["error"].get("code")) is not int
                             or not isinstance(value["error"].get("message"), str)):
        raise RelayError("chat_tool_upstream_invalid_response", 502, -32001)
    return value

class _RelayTransport(_Transport):
    response_limit = RESPONSE_LIMIT
    def _message(self, response, connection, identifier):
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        chunks = self._chunks(response, connection)
        if content_type == "application/json":
            return _envelope(_json(b"".join(chunks)), identifier)
        if content_type != "text/event-stream":
            raise RelayError("chat_tool_upstream_invalid_response", 502, -32001)

        # A POST SSE stream may remain open after its final response. Stop at
        # that response, not at EOF; never execute server-initiated requests.
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
                        message = _json(b"\n".join(data))
                        data = []
                        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                            raise RelayError("chat_tool_upstream_invalid_response", 502, -32001)
                        if ("id" in message and type(message["id"]) is type(identifier)
                                and message["id"] == identifier and "method" not in message):
                            return _envelope(message, identifier)
                elif line.startswith(b"data:"):
                    value = line[5:]
                    data.append(value[1:] if value.startswith(b" ") else value)
                elif line == b"data":
                    data.append(b"")
        raise RelayError("chat_tool_upstream_invalid_response", 502, -32001)

    def post(self, message, headers, *, notification=False):
        parts = self.parts
        factory = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        options = {"timeout": self.remaining()}
        if parts.scheme == "https":
            options["context"] = ssl.create_default_context()
        client = factory(parts.hostname, parts.port, **options)
        client._create_connection = self._connect
        response = None
        try:
            client.connect()
            connection = client.sock
            self._remember(connection)
            connection.settimeout(self.remaining())
            path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
            client.request("POST", path, json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), {
                "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "Accept-Encoding": "identity", "Connection": "close", **headers})
            connection.settimeout(self.remaining())
            response = client.getresponse()
            self.remaining()
            if 300 <= response.status < 400:
                raise RelayError("chat_tool_upstream_redirect_refused", 502, -32001)
            forwarded = {}
            for name, limit in _FORWARDED_HEADERS:
                values = response.headers.get_all(name, [])
                if len(values) > 1 or (values and not _header(values[0], limit)):
                    raise RelayError("chat_tool_upstream_invalid_response", 502, -32001)
                if values:
                    forwarded[name] = values[0]
            if notification:
                raw = b"".join(self._chunks(response, connection))
                if response.status in (200, 202, 204) and not raw:
                    return 202, None, forwarded
                if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json":
                    value = _json(raw)
                    if (isinstance(value, dict) and value.get("jsonrpc") == "2.0"
                            and (value.get("id") is None or type(value.get("id")) is int and value["id"] == 0)):
                        if (response.status == 200 and set(value) == {"jsonrpc", "id", "result"}
                                and value["result"] == {}):
                            return 202, None, forwarded
                        if "error" in value:
                            return response.status, _envelope(value, value.get("id")), forwarded
                raise RelayError("chat_tool_upstream_invalid_response", 502, -32001)
            if response.status == 200 or 400 <= response.status <= 599:
                value = self._message(response, connection, message["id"])
                if response.status != 200 and "error" not in value:
                    raise RelayError("chat_tool_upstream_invalid_response", 502, -32001)
                return response.status, value, forwarded
            raise RelayError("chat_tool_upstream_unavailable", 502, -32001)
        finally:
            if response is not None:
                response.close()
            client.close()
            with self.lock:
                self.active_socket = None

