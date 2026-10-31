"""Pure Python port of the frozen R3 draft-review contract (v2).

This module does not classify text, retrieve history, call a model, mutate a
database, approve truth, or deliver a reply. The host owns authenticated turn
binding, source projection, persistent attempt budgets, cancellation and final
delivery. Results are detached JSON-compatible dictionaries; the host must
snapshot caller-owned input before any asynchronous work.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any


DRAFT_REVIEW_VERSION = "companion-draft-review/v2"
DRAFT_REVIEW_LIMITS = {
    "maxDraftBytes": 32768,
    "maxCurrentBytes": 16384,
    "maxRecentMessages": 4,
    "maxRecentMessageBytes": 8192,
    "maxRecentBytes": 16384,
    "maxIssues": 4,
    "maxAnchorBytes": 4096,
    "maxQueryBytes": 768,
    "maxPlanBytes": 24576,
    "maxProjectedHistoryBytes": 8192,
}

_ROUTES = frozenset(("pass", "current_repair", "history_review"))
_KINDS = frozenset((
    "current_conflict", "current_repeat", "current_unsupported",
    "history_dependency", "history_request",
))
_HISTORY_KINDS = frozenset(("history_dependency", "history_request"))
_ROLES = frozenset(("user", "assistant", "tool"))
_MISSING = object()
# ECMAScript String.prototype.trim whitespace (Python str.strip differs).
_JS_WHITESPACE = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


class DraftReviewContractError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise DraftReviewContractError(code)


def _in(value: Any, choices: frozenset[str]) -> bool:
    return isinstance(value, str) and value in choices


def _exact_keys(value: Any, keys: tuple[str, ...], code: str) -> None:
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail(code)


def _scalar_text(value: str) -> str:
    """Accept valid UTF-16 pairs like JS; reject every unpaired surrogate."""
    out = []
    index = 0
    while index < len(value):
        cp = ord(value[index])
        if 0xD800 <= cp <= 0xDBFF:
            if index + 1 == len(value) or not 0xDC00 <= ord(value[index + 1]) <= 0xDFFF:
                raise UnicodeError("unpaired surrogate")
            cp = 0x10000 + ((cp - 0xD800) << 10) + ord(value[index + 1]) - 0xDC00
            out.append(chr(cp))
            index += 2
        elif 0xDC00 <= cp <= 0xDFFF:
            raise UnicodeError("unpaired surrogate")
        else:
            out.append(value[index])
            index += 1
    return "".join(out)


def _well_formed(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _scalar_text(value)
    except UnicodeError:
        return False
    return True


def _byte_length(value: str) -> int:
    return len(_scalar_text(value).encode("utf-8"))


def _text(value: Any, maximum: int, code: str, allow_empty: bool = False) -> str:
    if (not _well_formed(value)
            or (not allow_empty and not value.strip(_JS_WHITESPACE))
            or _byte_length(value) > maximum):
        _fail(code)
    return value


def _json_compatible(value: Any) -> Any:
    # JSON.stringify emits null for nonfinite JS numbers; raw JSON parsing below
    # still rejects NaN/Infinity spellings, just like JSON.parse.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_compatible(item) for key, item in value.items()}
    return value


def _json_bytes(value: Any) -> int:
    encoded = json.dumps(_json_compatible(value), ensure_ascii=False, separators=(",", ":"))
    # Well-formed JSON.stringify escapes lone surrogates and combines pairs.
    # This is required before validating individual issue fields so the error
    # code is the same as the JS validator's field-specific failure.
    parts = []
    index = 0
    while index < len(encoded):
        cp = ord(encoded[index])
        if (0xD800 <= cp <= 0xDBFF and index + 1 < len(encoded)
                and 0xDC00 <= ord(encoded[index + 1]) <= 0xDFFF):
            parts.append(chr(0x10000 + ((cp - 0xD800) << 10)
                             + ord(encoded[index + 1]) - 0xDC00))
            index += 2
        elif 0xD800 <= cp <= 0xDFFF:
            parts.append("\\u%04x" % cp)
            index += 1
        else:
            parts.append(encoded[index])
            index += 1
    return len("".join(parts).encode("utf-8"))


def _is_safe_integer(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return False
    return abs(value) <= 9007199254740991


def build_draft_review_input(value: Any = _MISSING, *, current_user: Any = _MISSING,
                            recent_context: Any = _MISSING, draft: Any = _MISSING) -> dict:
    """Build the bounded input without truncating current user or draft.

    Accept either a JS-shaped input dictionary or keyword arguments. Whole
    recent messages are selected from newest to oldest; all omissions count.
    """
    if value is not _MISSING:
        if not isinstance(value, dict) or any(
                field is not _MISSING for field in (current_user, recent_context, draft)):
            _fail("invalid_review_input")
        current_user = value.get("current_user", _MISSING)
        recent_context = value.get("recent_context", _MISSING)
        draft = value.get("draft", _MISSING)
    if recent_context is _MISSING:
        recent_context = []
    user_content = (current_user if isinstance(current_user, str)
                    else current_user.get("content") if isinstance(current_user, dict) else None)
    draft_content = (draft if isinstance(draft, str)
                     else draft.get("content") if isinstance(draft, dict) else None)
    _text(user_content, DRAFT_REVIEW_LIMITS["maxCurrentBytes"], "invalid_current_user")
    _text(draft_content, DRAFT_REVIEW_LIMITS["maxDraftBytes"], "invalid_complete_draft")
    if not isinstance(recent_context, list):
        _fail("invalid_recent_context")
    retained = []
    used = 0
    for row in reversed(recent_context):
        if (not isinstance(row, dict) or not _in(row.get("role"), _ROLES)
                or not _well_formed(row.get("content"))):
            _fail("invalid_recent_message")
        size = _byte_length(row["content"])
        if (len(retained) >= DRAFT_REVIEW_LIMITS["maxRecentMessages"]
                or size > DRAFT_REVIEW_LIMITS["maxRecentMessageBytes"]
                or used + size > DRAFT_REVIEW_LIMITS["maxRecentBytes"]):
            continue
        retained.insert(0, {"role": row["role"], "content": row["content"]})
        used += size
    return {
        "current_user": {"id": "u0", "role": "user", "content": user_content},
        "recent_context": [{"id": f"r{index}", **row} for index, row in enumerate(retained)],
        "draft": {"content": draft_content, "provenance": "unsent_unverified"},
        "coverage": {"recent_omitted_count": len(recent_context) - len(retained),
                     "draft_is_complete": True},
    }


def _validate_input(value: Any) -> dict:
    _exact_keys(value, ("current_user", "recent_context", "draft", "coverage"), "invalid_review_input")
    current = value["current_user"]
    _exact_keys(current, ("id", "role", "content"), "invalid_current_user")
    if current["id"] != "u0" or current["role"] != "user":
        _fail("invalid_current_user")
    _text(current["content"], DRAFT_REVIEW_LIMITS["maxCurrentBytes"], "invalid_current_user")
    draft = value["draft"]
    _exact_keys(draft, ("content", "provenance"), "invalid_complete_draft")
    _text(draft["content"], DRAFT_REVIEW_LIMITS["maxDraftBytes"], "invalid_complete_draft")
    if draft["provenance"] != "unsent_unverified":
        _fail("invalid_draft_provenance")
    coverage = value["coverage"]
    _exact_keys(coverage, ("recent_omitted_count", "draft_is_complete"), "invalid_input_coverage")
    if (not _is_safe_integer(coverage["recent_omitted_count"])
            or coverage["recent_omitted_count"] < 0 or coverage["draft_is_complete"] is not True):
        _fail("invalid_input_coverage")
    recent = value["recent_context"]
    if not isinstance(recent, list) or len(recent) > DRAFT_REVIEW_LIMITS["maxRecentMessages"]:
        _fail("invalid_recent_context")
    recent_bytes = 0
    for index, row in enumerate(recent):
        _exact_keys(row, ("id", "role", "content"), "invalid_recent_message")
        if row["id"] != f"r{index}" or not _in(row["role"], _ROLES):
            _fail("invalid_recent_message")
        _text(row["content"], DRAFT_REVIEW_LIMITS["maxRecentMessageBytes"], "invalid_recent_message", True)
        recent_bytes += _byte_length(row["content"])
    if recent_bytes > DRAFT_REVIEW_LIMITS["maxRecentBytes"]:
        _fail("invalid_recent_context")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def validate_draft_review_plan(raw: Any, value: Any) -> dict:
    """Validate syntax and references only; this does not approve factual truth."""
    _validate_input(value)
    plan = raw
    if isinstance(raw, str):
        _text(raw, DRAFT_REVIEW_LIMITS["maxPlanBytes"], "invalid_review_json")
        try:
            plan = json.loads(raw, parse_constant=_reject_json_constant)
        except (ValueError, TypeError):
            _fail("invalid_review_json")
    _exact_keys(plan, ("route", "issues", "recall_scope") if isinstance(plan, dict) and "recall_scope" in plan else ("route", "issues"), "invalid_review_plan")
    if "recall_scope" in plan and not _in(plan["recall_scope"], frozenset(("work", "life", "shared"))):
        _fail("invalid_recall_scope")
    if (_json_bytes(plan) > DRAFT_REVIEW_LIMITS["maxPlanBytes"] or not _in(plan["route"], _ROUTES)
            or not isinstance(plan["issues"], list)
            or len(plan["issues"]) > DRAFT_REVIEW_LIMITS["maxIssues"]):
        _fail("invalid_review_plan")
    evidence_by_id = {row["id"]: row for row in [value["current_user"], *value["recent_context"]]}
    seen = set()
    issues = []
    for issue in plan["issues"]:
        _exact_keys(issue, ("text", "kind", "current_evidence_ids", "query"), "invalid_review_issue")
        anchor = _text(issue["text"], DRAFT_REVIEW_LIMITS["maxAnchorBytes"], "invalid_draft_anchor")
        if _scalar_text(anchor) not in _scalar_text(value["draft"]["content"]):
            _fail("anchor_not_in_complete_draft")
        kind = issue["kind"]
        if not _in(kind, _KINDS):
            _fail("invalid_issue_kind")
        ids = issue["current_evidence_ids"]
        if (not isinstance(ids, list) or len(ids) > len(evidence_by_id)
                or any(not isinstance(eid, str) or eid not in evidence_by_id for eid in ids)
                or len(set(ids)) != len(ids)):
            _fail("invalid_current_evidence_ids")
        if kind in ("current_conflict", "current_repeat") and not ids:
            _fail("current_issue_requires_evidence")
        if kind == "current_conflict" and not any(evidence_by_id[eid]["role"] in ("user", "tool") for eid in ids):
            _fail("assistant_claim_cannot_prove_conflict")
        if kind == "history_request" and not any(evidence_by_id[eid]["role"] == "user" for eid in ids):
            _fail("history_request_requires_user_evidence")
        _text(issue["query"], DRAFT_REVIEW_LIMITS["maxQueryBytes"], "invalid_issue_query", kind not in _HISTORY_KINDS)
        if kind not in _HISTORY_KINDS and issue["query"] != "":
            _fail("current_issue_must_not_query_history")
        key = (_scalar_text(anchor), kind)
        if key in seen:
            _fail("duplicate_review_issue")
        seen.add(key)
        issues.append({"text": anchor, "kind": kind,
                       "current_evidence_ids": list(ids), "query": issue["query"]})
    route = ("history_review" if any(issue["kind"] in _HISTORY_KINDS for issue in issues)
             else "current_repair" if issues else "pass")
    if plan["route"] != route:
        _fail("route_issue_mismatch")
    return {"route": route, "issues": issues, **({"recall_scope": plan["recall_scope"]} if "recall_scope" in plan else {})}


def build_review_work(raw_plan: Any, value: Any) -> dict:
    plan = validate_draft_review_plan(raw_plan, value)
    selected = {eid for issue in plan["issues"] for eid in issue["current_evidence_ids"]}
    return {
        "version": DRAFT_REVIEW_VERSION,
        "route": plan["route"],
        "recall_scope": plan.get("recall_scope", "shared"),
        "issues": copy.deepcopy(plan["issues"]),
        "history_targets": [{"text": issue["text"], "kind": "uncertain", "query": issue["query"]}
                            for issue in plan["issues"] if issue["kind"] in _HISTORY_KINDS],
        "current_evidence": [copy.deepcopy(row) for row in [value["current_user"], *value["recent_context"]]
                             if row["id"] in selected],
        "review_limit": 1,
        "revision_limit": 1,
        "approval_provided": False,
    }


ONE_REVISION_INSTRUCTION = '这是本轮唯一一次发送前核对或改稿。先处理 issues 中当前矛盾和重复，再核对历史主张；当前用户明确更正与真实工具回执优于旧助手自称。history_request 表示用户请求尚待查证，不表示草稿诚实说不确定是错误；同题已尝试后仍无可靠证据，可以原样保留准确的不确定，不为修改而修改。旧助手说过、计划、幻想和承诺都不自证事情发生或完成。历史候选的相关性不证明同一事件，时间更晚也不自动证明推翻或履行。未命中、超时或内容未交付，既不证明从未发生，也不批准无据的肯定断言；只撤回、限定或明确表示未核实的具体断言，不凭空写相反事实。区分检索失败与成功但无匹配，不能声称已经完整查遍。当前明确改色等决定直接落实；仅为旧选择辩护的无据理由可随同纠正移除，不要删除用户明确问的独立历史问题或已经有据的正确内容。保留当下表达与新意愿，不强行回顾旧事。不要再调用核对、检索或其他工具；不要加入新的未经核实往事。严格输出 {"action":"keep"} 或 {"action":"replace","content":"完整公开回复"}；keep 仅在核对后仍适合时使用，宿主原样保留原文。'

# Exact source-export aliases ease host integration and parity testing.
buildDraftReviewInput = build_draft_review_input
validateDraftReviewPlan = validate_draft_review_plan
buildReviewWork = build_review_work
