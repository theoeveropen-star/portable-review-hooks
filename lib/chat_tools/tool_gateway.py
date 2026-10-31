"""Collapse one approved MCP server into a two-tool menu gateway (Beside only).

The model's tool directory then carries two names instead of the whole server.
The approved allowlist, upstream URL, credentials, timeouts, result passthrough
and per-call guard/accounting are unchanged: ``<prefix>_do`` forwards exactly the
``tools/call`` the flat tool would have made, after checking the action is an
approved upstream tool and its arguments fit that tool's own inputSchema.
Switch: ``tool_gateways.json`` next to this file (``enabled: false`` restores
the flat tools on the next adapter start).
"""
import json
import re
from pathlib import Path

CONFIG = Path(__file__).with_name("tool_gateways.json")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


class GatewayError(ValueError):
    pass


class Gateway:
    def __init__(self, server, item):
        self.server = server
        self.menu = item["menu"]
        self.do = item["do"]
        self.menu_description = item["menu_description"]
        self.do_description = item["do_description"]
        self.groups = [(g["name"], tuple(g["tools"])) for g in item["groups"]]
        # Upstream tools approved for the gateway only (the flat allowlist and
        # session pins stay as they were); notes mark actions others can see.
        self.extra_actions = tuple(item["extra_actions"])
        self.notes = dict(item["notes"])

    def allowed(self, server_tools):
        return frozenset(server_tools) | frozenset(self.extra_actions)

    def names(self):
        return (self.menu, self.do)

    def qualified(self):
        return tuple("mcp__" + self.server + "__" + name for name in self.names())

    def tools(self):
        group_names = [name for name, _ in self.groups] + ["其他"]
        return [
            {"name": self.menu, "description": self.menu_description,
             "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
                 "group": {"type": "string", "enum": group_names,
                           "description": "只看某一组；不填就列出全部。"}}}},
            {"name": self.do, "description": self.do_description,
             "inputSchema": {"type": "object", "additionalProperties": False, "required": ["action"],
                             "properties": {
                                 "action": {"type": "string", "description": "菜单里列出的工具名，例如 zombie。"},
                                 "arguments": {"type": "object", "description": "按菜单里该工具的 inputSchema 填写。"}}}},
        ]

    def group_of(self, name):
        for group, members in self.groups:
            if name in members:
                return group
        return "其他"

    def menu_result(self, upstream_tools, group=None):
        """upstream_tools: the already allowlist-filtered, decorated tool list."""
        entries = {}
        for tool in upstream_tools:
            note = self.notes.get(tool["name"])
            description = tool.get("description", "")
            entry = {"action": tool["name"],
                     "description": ("【" + note + "】" + description) if note else description,
                     "inputSchema": tool.get("inputSchema", {})}
            if note:
                entry["visible_to_others"] = note
            entries.setdefault(self.group_of(tool["name"]), []).append(entry)
        if group is not None:
            entries = {group: entries.get(group, [])}
        order = [name for name, _ in self.groups] + ["其他"]
        menu = [{"group": name, "tools": entries[name]} for name in order if name in entries]
        text = json.dumps({"how": "用 " + self.do + " 调用：action=工具名，arguments=按它的 inputSchema 填。",
                           "menu": menu}, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "structuredContent": {"menu": menu}, "isError": False}


def load(path=CONFIG, servers=()):
    """Enabled gateways for servers present in the registry; strict shape."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise GatewayError("tool_gateway_config_invalid") from None
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("servers"), dict):
        raise GatewayError("tool_gateway_config_invalid")
    present = {server.name: server for server in servers}
    gateways = {}
    for name, item in value["servers"].items():
        if (not isinstance(item, dict) or set(item) != {"enabled", "menu", "do", "menu_description", "do_description", "groups", "extra_actions", "notes"}
                or type(item["enabled"]) is not bool or not all(isinstance(item[k], str) and _NAME.fullmatch(item[k]) for k in ("menu", "do"))
                or item["menu"] == item["do"]
                or not all(isinstance(item[k], str) and 0 < len(item[k]) <= 600 for k in ("menu_description", "do_description"))
                or not isinstance(item["groups"], list)
                or any(not isinstance(g, dict) or set(g) != {"name", "tools"} or not isinstance(g["name"], str)
                       or not isinstance(g["tools"], list) or not all(isinstance(t, str) for t in g["tools"])
                       for g in item["groups"])
                or not isinstance(item["extra_actions"], list)
                or not all(isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", t) for t in item["extra_actions"])
                or not isinstance(item["notes"], dict)
                or not all(isinstance(k, str) and isinstance(v, str) and 0 < len(v) <= 80 for k, v in item["notes"].items())):
            raise GatewayError("tool_gateway_config_invalid")
        if not item["enabled"] or name not in present:
            continue
        server = present[name]
        if {item["menu"], item["do"]} & (set(server.tools) | set(item["extra_actions"])):
            raise GatewayError("tool_gateway_name_collision")
        gateways[name] = Gateway(name, item)
    return gateways


def validate(schema, value, path="arguments"):
    """Small JSON-Schema subset; unknown keywords are left to the upstream."""
    if not isinstance(schema, dict):
        return None
    expected = schema.get("type")
    kinds = expected if isinstance(expected, list) else [expected] if expected else []
    if kinds:
        def fits(kind):
            if kind == "integer":
                return type(value) is int
            if kind == "number":
                return type(value) in (int, float)
            if kind == "null":
                return value is None
            python = _TYPES.get(kind)
            return python is None or (isinstance(value, python) and not (python is not bool and type(value) is bool))
        if not any(fits(kind) for kind in kinds):
            return path + " should be " + "/".join(str(k) for k in kinds)
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        return path + " should be one of " + json.dumps(schema["enum"], ensure_ascii=False)
    if isinstance(value, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for key in schema.get("required", []) if isinstance(schema.get("required"), list) else []:
            if key not in value:
                return path + "." + str(key) + " is required"
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                return path + " has unknown field " + extra[0]
        for key, item in value.items():
            if key in props:
                error = validate(props[key], item, path + "." + key)
                if error:
                    return error
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            error = validate(schema["items"], item, path + "[" + str(index) + "]")
            if error:
                return error
    return None


def refusal(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}
