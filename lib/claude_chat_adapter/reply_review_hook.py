"""Native Stop hook: one host recall, one native continuation, one final.

Only the supplied public assistant message is read. This module never opens a
transcript, reasoning stream, personal Archive, or a second model process.
"""
import argparse
import copy
import hashlib
import json
import secrets
import re
from pathlib import Path
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat_tools.reply_review import (ReviewError, draft_id,
    review_instruction, select_review_targets, unavailable_review)
from claude_chat_adapter.persona_config import USER


HOOK_PATH = "/hooks/reply-review"
HOOK_METHOD = "companion/stop-review"
HOOK_VERSION = "companion-claude-stop-review/v1"
DECISION_VERSION = "companion-reply-review-decision/v1"
BINDING_VERSION = "companion-claude-review-source/v1"
_JS_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def native_trim(content):
    # Installed 2.1.284 Stop code uses JS String.trim, whose whitespace set
    # differs slightly from Python str.strip. Preserve all interior text.
    return content.strip(_JS_WHITESPACE)


def _digest(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def keep_record(token):
    return json.dumps({"version": DECISION_VERSION, "action": "keep_original", "token": token},
                      separators=(",", ":"))


def has_control(content):
    if "companion-reply-review-decision/" in content:
        return True
    try:
        value = json.loads(content)
        return isinstance(value, dict) and str(value.get("version", "")).startswith("companion-reply-review-decision/")
    except ValueError:
        return False


def offline_action_record(content):
    try:
        value = json.loads(content)
        return isinstance(value, dict) and (value == {"action": "keep"}
            or set(value) == {"action", "content"} and value.get("action") == "replace")
    except ValueError:
        return False


def public_receipt(report):
    """Private native source bindings never enter quota/UI responses."""
    if not isinstance(report, dict):
        return report
    result = dict(report)
    review = result.get("reply_review_hook")
    if isinstance(review, dict):
        result["reply_review_hook"] = {key: value for key, value in review.items()
            if not key.startswith("_") and key not in
            ("draft_digest", "accepted_digest", "draft_sequence", "accepted_sequence")}
    return result


def public_review_status(report):
    """Small canonical diagnostic; no native identity, content, or credentials."""
    review = report.get("reply_review_hook") if isinstance(report, dict) else None
    if not isinstance(review, dict):
        return None
    semantic = review.get("semantic_review") or {}
    diagnostic = review.get("review_diagnostic") or {}
    metadata = {**(semantic.get("metadata") or {}), **(diagnostic.get("metadata") or {})}
    safe = {}
    for key in ("elapsed_ms", "provider_request_attempts"):
        if type(metadata.get(key)) in (int, float) and 0 <= metadata[key] < 100000000:
            safe[key] = metadata[key]
    if type(metadata.get("completion_unknown")) is bool:
        safe["completion_unknown"] = metadata["completion_unknown"]
    if type(metadata.get("http_status")) is int and 100 <= metadata["http_status"] <= 599:
        safe["http_status"] = metadata["http_status"]
    allowed = {
        "error_category": {"timeout", "cancelled", "http", "http_rejected", "transport", "malformed",
                           "contract", "contract_mismatch", "capacity", "configuration", "unavailable", "source_inflight"},
        "failure_stage": {"provider", "provider_transport", "provider_response", "plan_validation",
                          "input_validation", "relay_transport", "brain_dependency", "history_retrieval"},
    }
    for key in ("error_category", "failure_stage"):
        value = metadata.get(key)
        if isinstance(value, str) and value in allowed[key]:
            safe[key] = value
    if isinstance(review.get("retrieval_observation"), dict):
        from .draft_review_host import history_observation
        safe["retrieval_observation"] = history_observation(review["retrieval_observation"])
    outcome = review.get("revision_outcome")
    if isinstance(outcome, dict):
        clean = {"version": "companion-review-outcome/v1"}
        for key in ("requested", "completed", "content_changed", "support_gap", "retrieval_started"):
            if key in outcome and (type(outcome[key]) is bool or key in ("retrieval_started", "content_changed") and outcome[key] is None):
                clean[key] = outcome[key]
        if type(outcome.get("history_evidence_count")) is int and 0 <= outcome["history_evidence_count"] <= 16:
            clean["history_evidence_count"] = outcome["history_evidence_count"]
        if outcome.get("decision") in ("keep_original", "replace", "none"):
            clean["decision"] = outcome["decision"]
        safe["revision_outcome"] = clean
    error = diagnostic.get("error") or semantic.get("error")
    if error is not None and (not isinstance(error, str) or not
            re.fullmatch(r"(?:semantic|reviewer|reply_review|draft_review|brain|history)_[a-z0-9_]{1,80}", error)):
        error = "reply_review_unavailable"
    unavailable = (review.get("review_status") == "unavailable" or
                   semantic.get("status") in ("unavailable", "rejected"))
    return {"status": "unavailable" if unavailable else "ok", "mode": review.get("final_review"),
            "error": error, "metadata": safe, "approval_provided": False}


# Tools that return her original words (Archive / inbox / memory sources); 工作台 and memory
# cards are not 原话.
RAW_TEXT_TOOLS = {"read_archive", "find_archive_sources", "open_memory_source",
                  "open_window_memory_source", "open_story_source", "read_window_inbox",
                  "companion_archive_search", "archive_search", "search_archive"}
ORIGINAL_TEXT_TRIGGERS = {"不记得", "没印象", "手边没有", "翻不到", "没翻到", "没找到", "没搜到"}


SEALED_NOTE = "（审查已经开始，这一轮不能再调用任何工具；要查的资料就在终稿里照实说这轮还没查、下一轮去查。）"


class ReplyReviewTurn:
    def __init__(self, adapter, row, window, client, reserved_pending_id, *, native_api_turn=True):
        if type(native_api_turn) is not bool:
            raise ReviewError("reply_review_native_channel_invalid")
        self.adapter, self.session, self.turn = adapter, row["session_id"], row["client_message_id"]
        self.window, self.client = window, client
        self.native_id = None
        self.native_api_turn = native_api_turn
        self.user_text = row["user_text"]
        self.last_decision = None
        self.budget = None
        self.input_ceiling = None
        self.public_message = None
        self.public_sequence = 0
        self.public_ready = threading.Condition(adapter.lock)
        self.original_public = None
        self.keep_control = None
        self.keep_token = None
        from .draft_review_host import create_turn_reviewer
        self.semantic = create_turn_reviewer(adapter, row, window, client)
        self.recent_context = []
        from .draft_review_host import opening_canonical_context
        self.opening_context = opening_canonical_context(adapter, row) if self.semantic is not None else []
        from chat_tools.review_evidence import image_presence
        self.opening_context += image_presence(any(a.get("mime", "").startswith("image/") for a in row.get("attachments", [])))
        self.tool_context = []
        self.tool_omitted = 0
        self.tool_inflight = 0
        self.tools_sealed = False
        self.fridge_match = None
        self.fridge_searched_memory = False
        self.raw_text_searched = False

    def forward_tool(self, server, value, notification, callback):
        if self.semantic is None or notification or value.get("method") != "tools/call":
            return callback()
        from chat_tools.description_relay import RelayError
        from chat_tools.portable import portable_result
        params = value.get("params", {})
        with self.adapter.lock:
            _, state = self._load()
            self.tools_sealed = self.tools_sealed or state.get("tools_sealed") is True
            if self.tools_sealed:
                self.adapter.db.execute("UPDATE claude_turns SET guard_error=? WHERE session_id=? AND client_message_id=?",
                    ("reply_review_tools_sealed", self.session, self.turn))
                self.adapter.db.commit()
                # This is a small fatal JSON-RPC refusal, not an executed tool.
                if self.budget is not None:
                    with self.budget.lock:
                        self.budget.charge({"error": {"code": -32001, "message": "reply_review_tools_sealed"}})
                raise RelayError("reply_review_tools_sealed")
            self.tool_inflight += 1
        record = None
        try:
            result = callback()
            status, response, _ = result
            projected = portable_result(server.name, params.get("name"), response)
            if (status == 200 and isinstance(response, dict) and "error" not in response
                    and isinstance(response.get("result"), dict) and not response["result"].get("isError", False)
                    and params.get("name") in {"search_memories", "companion_archive_search", "archive_search", "search_memories", "search_archive"}):
                self.fridge_searched_memory = True
            if (status == 200 and isinstance(response, dict) and "error" not in response
                    and isinstance(response.get("result"), dict) and not response["result"].get("isError", False)
                    and server.name in ("house_brain", "house_heart") and params.get("name") in RAW_TEXT_TOOLS):
                self.raw_text_searched = True
            # Only the final projected response that the native model receives
            # is evidence. An archived oversized body is not secretly included.
            record = json.dumps({"tool": server.name + "." + str(params.get("name")),
                "arguments": params.get("arguments", {}), "http_status": status,
                "delivered_response": projected}, ensure_ascii=False, separators=(",", ":"))
            from .review_tool_sources import project_tool_record
            record = project_tool_record(record)
            key = getattr(self.semantic.client, "token", None)
            if key:
                record = record.replace(key, "[credential withheld]")
            return result
        finally:
            with self.public_ready:
                self.tool_inflight -= 1
                if record is None or len(record.encode()) > 8192:
                    self.tool_omitted += 1
                else:
                    self.tool_context.append({"role": "tool", "content": record})
                    if len(self.tool_context) > 32:
                        self.tool_context.pop(0)
                        self.tool_omitted += 1
                self.public_ready.notify_all()

    def project_review_feedback(self, body):
        """Remove only the duplicate of this host's nonce-bound Stop reason.

        Claude 2.1.284 expands one JSON block decision into a feedback and an
        error attachment. Native history/bindings stay exact; only the outbound
        model request loses the identical error copy. Other attachments stay.
        """
        if self.semantic is None or not self.last_decision:
            return body
        reason = self.last_decision.get("reason")
        if not reason or not self.keep_control or self.keep_control not in reason:
            return body
        with self.adapter.lock:
            _, state = self._load()
            if (state.get("state") != "revision_requested"
                    or state.get("_native_binding", {}).get("instruction_sha256") != _digest(reason)):
                return body
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or messages[-1].get("role") != "user":
            return body
        blocks = messages[-1].get("content")
        string_content = isinstance(blocks, str)
        if string_content:
            blocks = [{"type": "text", "text": blocks}]
        if not isinstance(blocks, list):
            return body
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        if (sum(t.count(reason) for t in texts) != 2
                or not any("Stop hook feedback:\n" + reason in t for t in texts)):
            return body
        pattern = re.compile(r'<system-reminder>\r?\nStop hook blocking error from command: [^\r\n]+?:[ \t]*(?:\r?\n)?'
                             + re.escape(reason) + r'\r?\n</system-reminder>')
        matches = [(i, pattern.search(b.get("text", ""))) for i, b in enumerate(blocks)
                   if b.get("type") == "text"]
        matches = [(i, match) for i, match in matches if match]
        if len(matches) != 1:
            return body
        projected = copy.deepcopy(body)
        if string_content:
            projected["messages"][-1]["content"] = copy.deepcopy(blocks)
        index, match = matches[0]
        block = projected["messages"][-1]["content"][index]
        block["text"] = block["text"][:match.start()] + block["text"][match.end():]
        if not block["text"].strip():
            del projected["messages"][-1]["content"][index]
            if "cache_control" in block and projected["messages"][-1]["content"]:
                projected["messages"][-1]["content"][-1].setdefault("cache_control", block["cache_control"])
        if string_content:
            projected["messages"][-1]["content"] = "".join(b.get("text", "") for b in projected["messages"][-1]["content"])
        return projected

    def admit_native_request(self):
        """One actual native revision request, reserved before upstream I/O."""
        if self.semantic is None:
            return
        if not self.native_api_turn:
            raise ReviewError("semantic_revision_request_channel_mismatch")
        with self.adapter.lock:
            report, state = self._load()
            if state.get("tools_sealed") is not True:
                return
            if (state.get("state") != "revision_requested"
                    or state.get("native_revision_requests", 0) != 0):
                raise ReviewError("semantic_revision_request_rejected")
            state["native_revision_requests"] = 1
            self._save(report, state)

    def observe_public(self, event):
        if event.get("type") != "assistant" or event.get("parent_tool_use_id"):
            return
        message = event.get("message", {})
        blocks = message.get("content", [])
        identifier = event.get("uuid") or message.get("id")
        if not isinstance(blocks, list) or not isinstance(identifier, str) or not identifier:
            raise ReviewError("reply_review_public_message_invalid")
        texts = [block.get("text") for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
        if any(not isinstance(text, str) for text in texts):
            raise ReviewError("reply_review_public_message_invalid")
        joined = "\n".join(texts)
        normalized = native_trim(joined)
        if normalized:
            select_review_targets(normalized)  # Same bounded UTF-8 content contract.
        view = {"id": identifier, "message_id": message.get("id"), "text": normalized,
                "projections": frozenset((joined, normalized, texts[-1], native_trim(texts[-1]))) if texts else frozenset()}
        with self.public_ready:
            # Never concatenate different assistant messages or read anything
            # except these public text blocks. Identical replay is idempotent.
            if self.public_message is not None and all(self.public_message.get(k) == v for k, v in view.items()):
                return
            self.public_sequence += 1
            self.public_message = {**view, "sequence": self.public_sequence}
            self.public_ready.notify_all()

    def _bound_public(self, content):
        # The native subprocess writes the assistant event before invoking Stop.
        # Allow a small bounded pipe-reader race, releasing the adapter lock.
        deadline = time.monotonic() + 0.4
        while self.public_message is None or self.public_message["text"] != content:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReviewError("reply_review_public_message_mismatch")
            self.public_ready.wait(remaining)
        return self.public_message

    def _fridge_verdicts(self, notes, content, host_recall=None):
        verdicts = []
        for hit in notes:
            with self.adapter.lock:
                raw = self.adapter.db.execute("SELECT rule_snapshot_json FROM claude_fridge_hits WHERE id=?",
                                              (hit["id"],)).fetchone()
            rule = json.loads(raw[0]) if raw else {}
            triggers = [t for t in rule.get("triggers") or [] if isinstance(t, str)]
            if rule.get("kind") == "memory" or "原话" in hit.get("note", "") or set(triggers) & ORIGINAL_TEXT_TRIGGERS:
                ok = self.raw_text_searched or bool(host_recall and host_recall["found"])
                reason = ("这一轮翻过原话" if self.raw_text_searched else "宿主已替你翻了原话，见核对资料") if ok else (
                    "宿主已替你翻过原话，没翻到；照实说没翻到或问" + USER + "，别编，也不用再调工具" if host_recall else
                    "这一轮没有翻原话（档案/原文）就说了没有；现在已不能再翻，别说没有/不记得，改成照实说这轮还没翻、下一轮去翻")
                verdicts.append({"hit": hit, "note": hit["note"], "category": "original_text", "violated": not ok,
                                 "reason": reason})
            else:
                # The matcher already applied the rule's own context guards; a remaining
                # match means the draft says the very thing the note is about.
                word = next((t for t in triggers if t in content), None)
                verdicts.append({"hit": hit, "note": hit["note"], "category": rule.get("kind") or "literal", "violated": True,
                                 "reason": ("草稿里写了「" + word + "」") if word else "草稿碰到了这张纸条"})
        return verdicts

    def bind_budget(self, budget, input_ceiling):
        self.budget, self.input_ceiling = budget, input_ceiling

    def _bounded_decision(self, receipt, state, *, reminder="", fridge_only=False, allow_keep=True):
        if allow_keep:
            self.keep_token = secrets.token_urlsafe(18)
            self.keep_control = keep_record(self.keep_token)
            choice = ("\n" + SEALED_NOTE + "\n保留原稿只输出下一行；有实质修改则输出完整终稿：\n"
                      + self.keep_control + "\n")
        else:
            # A violated fridge note is not optional: no one-line KEEP shortcut.
            self.keep_token = self.keep_control = None
            choice = "\n" + SEALED_NOTE + "\n先照纸条做到，再输出完整终稿；这一条不能原样保留。\n"
        material = "" if fridge_only else self.semantic.instruction(receipt) if self.semantic is not None else review_instruction(receipt)
        decision = {"decision": "block", "reason": material + reminder + choice}
        if self.semantic is not None and not fridge_only and self.semantic.last_material.get("material_status") != "available":
            state["review_material"] = {**self.semantic.last_material, "delivered": False}
            state["evidence_delivery"] = "actionable_material_unavailable"
            return None
        if self.budget is None:
            if self.semantic is not None and not fridge_only:
                state["review_material"] = {**self.semantic.last_material, "delivered": True}
            state["fridge_included"] = bool(reminder)
            state["_native_binding"]["instruction_sha256"] = _digest(decision["reason"])
            return decision
        from .turn_budget import wire_bound
        envelope = lambda result: {"jsonrpc": "2.0", "id": "chat-review-stop", "result": result}
        with self.budget.lock:
            if (reminder and not fridge_only and wire_bound(envelope(decision)) > self.budget.available(self.input_ceiling)):
                # Existing review evidence has priority. Notes never displace it.
                reminder = ""
                decision["reason"] = material + choice
            if wire_bound(envelope(decision)) > self.budget.available(self.input_ceiling):
                if fridge_only:
                    state["fridge_included"] = False
                    return None
                if self.semantic is not None:
                    # No anchorless generic correction. Preserve the source-bound
                    # original and report unavailable; do not spend a native turn.
                    state["evidence_delivery"] = "capacity_limited"
                    state["review_material"] = {**self.semantic.last_material, "delivered": False}
                    return None
                # Use the existing finish reserve for one small correction;
                # do not raise the input ceiling or inject oversized evidence.
                decision["reason"] = ("核对资料未装下，不代表通过。只按已有证据修具体问题，保留准确的未知，不重查。"
                    + ("保留原稿只输出下一行，否则输出完整终稿：\n" + self.keep_control + "\n" if allow_keep
                       else reminder + choice))
                state["evidence_delivery"] = "capacity_limited"
            else:
                state["evidence_delivery"] = "within_existing_turn_budget"
            self.budget.charge(envelope(decision))
        state["fridge_included"] = bool(reminder)
        if self.semantic is not None and not fridge_only:
            delivered = state.get("evidence_delivery") != "capacity_limited"
            state["review_material"] = {**self.semantic.last_material, "delivered": delivered}
        state["_native_binding"]["instruction_sha256"] = _digest(decision["reason"])
        return decision

    def _original(self, state):
        original = self.original_public
        binding = state.get("_native_binding", {})
        if (original is None or binding.get("version") != BINDING_VERSION
                or binding.get("native_id") != self.native_id or binding.get("turn_id") != self.turn
                or binding.get("window") != self.window or binding.get("session") != self.session
                or state.get("draft_sequence") != original["sequence"]
                or state.get("draft_digest") != _digest(original["text"])
                or binding.get("draft_sha256") != _digest(original["text"])
                or binding.get("draft_message_id") != original["message_id"]):
            raise ReviewError("reply_review_original_unbound")
        return original

    @staticmethod
    def initial_state():
        return {"version": HOOK_VERSION, "enabled": True, "state": "awaiting_draft",
                "review_attempts": 0, "revision_count": 0, "approval_provided": False}

    def _load(self):
        # Called under the adapter's existing lock. The attempt mark is durable
        # before network I/O, so retries/process loss cannot open a second read.
        columns = "status,receipt_json,guard_error" if self.semantic is not None else "status,receipt_json"
        row = self.adapter.db.execute("SELECT " + columns + " FROM claude_turns "
            "WHERE session_id=? AND client_message_id=?", (self.session, self.turn)).fetchone()
        active = self.adapter.active.get((self.session, self.turn))
        if not row or row[0] != "running" or not active or active["cancel"].is_set():
            raise ReviewError("reply_review_turn_inactive")
        if self.semantic is not None and row[2]:
            raise ReviewError("reply_review_turn_guarded")
        report = json.loads(row[1] or "{}")
        state = report.get("reply_review_hook")
        if not isinstance(state, dict) or state.get("version") != HOOK_VERSION or state.get("enabled") is not True:
            raise ReviewError("reply_review_turn_unbound")
        return report, state

    def _save(self, report, state):
        report["reply_review_hook"] = state
        changed = self.adapter.db.execute("UPDATE claude_turns SET receipt_json=? "
            "WHERE session_id=? AND client_message_id=? AND status='running'",
            (json.dumps(report, ensure_ascii=False), self.session, self.turn)).rowcount
        if changed != 1:
            raise ReviewError("reply_review_turn_inactive")
        self.adapter.db.commit()

    def decide(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"session_id", "last_assistant_message", "stop_hook_active"}
                or payload["session_id"] != self.native_id or type(payload["stop_hook_active"]) is not bool):
            raise ReviewError("reply_review_hook_binding_invalid")
        content = payload["last_assistant_message"]
        # Legacy selection is unchanged outside the explicitly selected R3
        # session. R3 sends the complete draft to its bounded semantic contract.
        targets = [] if self.semantic is not None else select_review_targets(content, self.user_text)
        digest = _digest(content)
        with self.adapter.lock:
            report, state = self._load()
            public = self._bound_public(content)
            report, state = self._load()  # A cancelled turn may have changed while the pipe reader caught up.
            if state["state"] == "accepted":
                if state.get("accepted_digest") != digest or state.get("accepted_sequence") != public["sequence"]:
                    raise ReviewError("reply_review_final_changed")
                return {}
            if state["state"] == "revision_requested":
                if not payload["stop_hook_active"]:
                    # A repeated first callback may receive the same decision,
                    # but can neither re-query nor pretend a revision happened.
                    if (state.get("draft_digest") == digest and state.get("draft_sequence") == public["sequence"]
                            and self.last_decision is not None):
                        return dict(self.last_decision)
                    raise ReviewError("reply_review_revision_unbound")
                if public["sequence"] <= state.get("draft_sequence", 0):
                    raise ReviewError("reply_review_revision_unbound")
                original = self._original(state)
                if self.semantic is not None:
                    if self.native_api_turn:
                        if (state.get("revision_proof_mode") != "api_request_reservation"
                                or type(state.get("native_revision_requests")) is not int
                                or state["native_revision_requests"] != 1):
                            raise ReviewError("reply_review_revision_unbound")
                    elif (state.get("revision_proof_mode") != "native_stop_public_message"
                            or state.get("native_revision_requests") is not None
                            or type(state.get("native_revision_observations")) is not int
                            or state.get("native_revision_observations") != 0
                            or public.get("id") == original.get("id")
                            or public.get("message_id") == original.get("message_id")):
                        raise ReviewError("reply_review_revision_unbound")
                keep = self.keep_control is not None and content == self.keep_control
                if not keep and (has_control(content) or self.keep_token and self.keep_token in content):
                    raise ReviewError("reply_review_control_unbound")
                if self.semantic is not None and offline_action_record(content):
                    raise ReviewError("reply_review_control_unbound")
                if not isinstance(public.get("message_id"), str) or not public["message_id"]:
                    raise ReviewError("reply_review_public_message_invalid")
                state.update(state="accepted", accepted_digest=digest, revision_count=1,
                             accepted_sequence=public["sequence"], final_review="revision_no_retrieval",
                             decision="keep_original" if keep else "replace",
                             content_changed=False if keep else content != original["text"])
                if isinstance(state.get("revision_outcome"), dict):
                    state["revision_outcome"].update(requested=state.get("continuation_reason") != "fridge",
                        completed=state.get("continuation_reason") != "fridge",
                        decision=state["decision"], content_changed=state["content_changed"])
                if state.get("fridge_included") and self.fridge_match:
                    self.adapter.fridge.mark_delivered("claude-owner", self.session, self.turn,
                        [hit["id"] for hit in self.fridge_match["notes"]])
                    state["fridge_notes"]["delivered_hit_ids"] = [hit["id"] for hit in self.fridge_match["notes"]]
                state["_native_binding"].update(decision_sha256=digest,
                    decision_message_id=public["message_id"], selection=state["decision"],
                    delivered_sha256=_digest(original["text"] if keep else content))
                if self.semantic is not None and not self.native_api_turn:
                    # Subscription traffic has no host API reservation ledger.
                    # Bind its one observed native continuation, never invent an
                    # upstream request count or accept an unobserved final.
                    state["native_revision_observations"] = 1
                    state["_native_revision_binding"] = {
                        "version": "companion-native-stop-revision/v1", "native_id": self.native_id,
                        "session": self.session, "window": self.window, "turn_id": self.turn,
                        "draft_message_id": original["message_id"],
                        "decision_message_id": public["message_id"], "decision_sha256": digest,
                        "stop_hook_active": True}
                self._save(report, state)
                return {}
            if state["state"] != "awaiting_draft" or state.get("review_attempts") != 0 or payload["stop_hook_active"]:
                raise ReviewError("reply_review_attempt_already_owned")
            if has_control(content):
                raise ReviewError("reply_review_control_unbound")
            if not isinstance(public.get("message_id"), str) or not public["message_id"]:
                raise ReviewError("reply_review_public_message_invalid")
            self.original_public = dict(public)
            state.update(state="reviewing", draft_digest=digest, review_attempts=1,
                         draft_sequence=public["sequence"], selected_targets=len(targets),
                         _native_binding={"version": BINDING_VERSION, "native_id": self.native_id,
                            "session": self.session, "window": self.window, "turn_id": self.turn,
                            "draft_message_id": public["message_id"], "draft_sha256": digest})
            self._save(report, state)
            if self.semantic is not None:
                self.tools_sealed = True
                state["tools_sealed"] = True
                state["revision_proof_mode"] = ("api_request_reservation" if self.native_api_turn
                                                else "native_stop_public_message")
                if not self.native_api_turn:
                    state.update(native_revision_requests=None, native_revision_observations=0)
                state["semantic_review"] = {"version": "companion-draft-review/v2", "status": "reserved",
                                            "approval_provided": False}
                self._save(report, state)
                # Stop normally follows all tool responses. Bound the unusual
                # in-flight race without claiming an unfinished tool completed.
                deadline = time.monotonic() + 1.0
                while self.tool_inflight and time.monotonic() < deadline:
                    self.public_ready.wait(max(0, deadline - time.monotonic()))
                report, state = self._load()
                source_inflight = self.tool_inflight
                recent = copy.deepcopy(self.recent_context + self.tool_context + self.opening_context)
                omitted = self.tool_omitted
                state["tools_inflight_at_review"] = source_inflight
                self._save(report, state)
                if not source_inflight and self.budget is not None:
                    self.budget.seal_for_review()
        identifier = draft_id(self.window, self.turn, content)
        # Both rule matching and R3 inspect the same bound public draft. The
        # matcher performs no model calls and cannot change R3's route.
        fridge = getattr(self.adapter, "fridge", None)
        if fridge is not None and self.semantic is not None:
            self.fridge_match = fridge.match("claude-owner", self.session, self.turn, content,
                previous_user=self.user_text, searched_memory=self.fridge_searched_memory)
        if self.semantic is None:
            try:
                receipt = self.client.review(self.window, self.turn, identifier, content, targets)
            except Exception:
                receipt = unavailable_review(self.turn, identifier)
            needs_revision = (bool(targets) and receipt.get("status") == "ok"
                              and bool(receipt.get("review", {}).get("evidence")))
        else:
            from .draft_review_host import local_receipt, failure_diagnostic, revision_basis
            if source_inflight:
                self.semantic.result = {"status": "unavailable", "plan": None, "work": None,
                    "error": "semantic_review_source_inflight", "metadata": {"provider_request_attempts": 0,
                        "error_category": "source_inflight", "failure_stage": "input_validation"}}
                result = self.semantic.result
            else:
                try:
                    result = self.semantic.classify(self.user_text, recent, content, omitted)
                except Exception as error:
                    diagnostic = failure_diagnostic(error)
                    self.semantic.result = {"status": "unavailable", "plan": None, "work": None,
                        "error": diagnostic["error"], "metadata": {"provider_request_attempts": None,
                            **diagnostic["metadata"]}}
                    result = self.semantic.result
            work = result.get("work") or {}
            route = work.get("route") if result.get("status") == "ok" else None
            # Recheck live turn/cancel ownership between classifier and recall.
            with self.adapter.lock:
                report, state = self._load()
                if state.get("state") != "reviewing" or state.get("draft_digest") != digest:
                    raise ReviewError("reply_review_attempt_state_changed")
                state["semantic_review"] = self.semantic.summary()
                state["selected_targets"] = len(work.get("history_targets", []))
                if route == "history_review":
                    state["semantic_history_attempt_reserved"] = True
                self._save(report, state)
            diagnostic = {"error": result.get("error"), "metadata": {
                key: (result.get("metadata") or {})[key] for key in
                ("error_category", "http_status", "failure_stage", "elapsed_ms", "provider_request_attempts", "completion_unknown")
                if key in (result.get("metadata") or {})}}
            receipt = (self.semantic.history(self.client, self.window, self.turn, identifier, content)
                       if route == "history_review" else local_receipt(self.turn, identifier, route,
                           unavailable=route is None, diagnostic=diagnostic))
            from .draft_review_host import apply_reverse_recall
            receipt = apply_reverse_recall(receipt, self.semantic.result)
            # Classification failure supplies no concrete issue. Keep the exact
            # bound draft and the failure diagnostic; never spend a KEEP turn
            # merely to restate uncertainty or manufacture semantic approval.
            basis = revision_basis(work, receipt)
            current_issue = basis["current_issue_count"] > 0
            historical = receipt.get("review", {})
            needs_revision = basis["needs_revision"]
            honest_miss = (historical.get("retrieval_status") == "no_match"
                and bool(result.get("name_guard", {}).get("required_queries"))
                and all(re.search(r"没找回|没找到", issue.get("text", ""))
                    and issue.get("query") in result["name_guard"]["required_queries"]
                    for issue in work.get("issues", [])))
            if honest_miss and not current_issue:
                needs_revision = False
        review = receipt["review"]
        with self.adapter.lock:
            report, state = self._load()
            if state["state"] != "reviewing" or state.get("draft_digest") != digest:
                raise ReviewError("reply_review_attempt_state_changed")
            state.update(review_status=receipt.get("status"), retrieval_status=review.get("retrieval_status"),
                         retrieval_started=review.get("retrieval_started") if type(review.get("retrieval_started")) is bool else None,
                         evidence_count=len(review.get("evidence", [])),
                         reuse_count=review.get("reuse_count", 0))
            state["revision_outcome"] = {"version": "companion-review-outcome/v1", "requested": False,
                "completed": False, "content_changed": False, "decision": "none",
                "retrieval_started": state["retrieval_started"], "history_evidence_count": state["evidence_count"],
                "support_gap": bool(self.semantic is not None and basis["support_gap"])}
            if review.get("diagnostic"):
                state["review_diagnostic"] = review["diagnostic"]
            if self.semantic is not None:
                state["semantic_review"] = self.semantic.summary()
                if getattr(self.semantic, "history_observation", None) is not None:
                    state["retrieval_observation"] = copy.deepcopy(self.semantic.history_observation)
                if basis["support_gap"]:
                    state["support_gap"] = {"status": "unresolved", "revision_required": False,
                        "issue_count": basis["unresolved_history_issue_count"],
                        "history_status": review.get("retrieval_status", "unknown")}
            elif isinstance(review.get("retrieval_observation"), dict):
                from .draft_review_host import history_observation
                state["retrieval_observation"] = history_observation(review["retrieval_observation"])
            host_recall = None
            if self.semantic is not None:
                from .draft_review_host import reverse_recall_queries
                guess = reverse_recall_queries(self.semantic.result) if route == "history_review" else []
                if guess and isinstance(review.get("reverse_recall"), dict):
                    host_recall = {"queries": guess, "found": bool(review.get("evidence"))}
                    state["reverse_recall"] = {"queries_count": len(guess), "found": host_recall["found"],
                                               "retrieval_status": review.get("retrieval_status")}
            verdicts = self._fridge_verdicts(self.fridge_match["notes"], content, host_recall) if self.fridge_match and self.fridge_match["notes"] else []
            violated = [v for v in verdicts if v["violated"]]
            # Only a note the draft actually broke reaches the model: her words, one reason line.
            reminder = ("\n" + "\n".join(v["note"] + "\n（" + v["reason"] + "）" for v in violated) + "\n") if violated else ""
            if violated:
                self.fridge_match = {**self.fridge_match, "notes": [v["hit"] for v in violated]}
            if self.fridge_match:
                state["fridge_notes"] = {"matched": len(self.fridge_match["hits"]),
                    "selected": len(verdicts), "violated": len(violated),
                    "passed_silently": len(verdicts) - len(violated), "model_calls": 0,
                    "verdicts": [{"hit_id": v["hit"]["id"], "category": v["category"],
                                  "violated": v["violated"], "reason": v["reason"]} for v in verdicts]}
            state["continuation_reason"] = "review_and_fridge" if needs_revision and reminder else "review" if needs_revision else "fridge" if reminder else "none"
            miss = ""
            if host_recall is not None and not host_recall["found"] and not needs_revision:
                # Reverse recall looked and found nothing: say so in the same turn
                # (tools are sealed), instead of leaving "下一轮去翻" in the reply.
                from .draft_review_host import reverse_recall_lead
                miss = "\n" + reverse_recall_lead({"queries": host_recall["queries"], "found": 0}) + "\n"
                state["continuation_reason"] = "reverse_recall_miss_and_fridge" if reminder else "reverse_recall_miss"
            if not needs_revision and (reminder or miss):
                decision = self._bounded_decision(receipt, state, reminder=miss + reminder, fridge_only=True, allow_keep=not reminder)
                state["fridge_included"] = bool(reminder) and state.get("fridge_included") is True
                if decision is not None:
                    state.update(state="revision_requested")
                    state["revision_outcome"]["content_changed"] = None
                    self.last_decision = decision
                    self._save(report, state)
                    return decision
            if not needs_revision:
                # This local selection made no historical claim to verify.
                # Preserve an unavailable receipt honestly; do not relabel it
                # as a successful semantic review or grant approval.
                mode = ("semantic_unavailable" if receipt.get("status") == "unavailable" else
                        "semantic_pass" if self.semantic is not None and route == "pass" else
                        "history_no_evidence" if (self.semantic is not None or targets) else
                        "present_no_history_dependency")
                state.update(state="accepted", accepted_digest=digest,
                             accepted_sequence=state["draft_sequence"],
                             final_review=mode)
                state.pop("_native_binding", None)
                decision = {}
            else:
                state.update(state="revision_requested")
                decision = self._bounded_decision(receipt, state, reminder=reminder, allow_keep=not reminder)
                if decision is None:
                    state.update(state="accepted", accepted_digest=digest,
                        accepted_sequence=state["draft_sequence"], final_review="semantic_material_unavailable",
                        review_status="unavailable")
                    state["review_diagnostic"] = {"error": "semantic_review_material_not_delivered",
                        "metadata": {"error_category": "capacity", "completion_unknown": False}}
                    state.pop("_native_binding", None)
                    decision = {}
                else:
                    state["revision_outcome"].update(requested=True, content_changed=None)
            self.last_decision = decision
            self._save(report, state)
        return decision

    def accepted_final(self, native_result):
        with self.adapter.lock:
            _, state = self._load()
            public = self.public_message
            if (state.get("state") != "accepted" or public is None
                    or state.get("accepted_sequence") != public["sequence"]
                    or state.get("accepted_digest") != _digest(public["text"])
                    or native_result not in public["projections"]):
                raise ReviewError("reply_review_terminal_unverified")
            if self.semantic is not None and state.get("revision_count") == 1:
                if self.native_api_turn:
                    if (state.get("revision_proof_mode") != "api_request_reservation"
                            or type(state.get("native_revision_requests")) is not int
                            or state["native_revision_requests"] != 1):
                        raise ReviewError("reply_review_revision_unbound")
                else:
                    binding = state.get("_native_revision_binding")
                    original = self._original(state)
                    expected = {"version": "companion-native-stop-revision/v1", "native_id": self.native_id,
                        "session": self.session, "window": self.window, "turn_id": self.turn,
                        "draft_message_id": original["message_id"],
                        "decision_message_id": public["message_id"],
                        "decision_sha256": _digest(public["text"]), "stop_hook_active": True}
                    if (state.get("revision_proof_mode") != "native_stop_public_message"
                            or state.get("native_revision_requests") is not None
                            or type(state.get("native_revision_observations")) is not int
                            or state["native_revision_observations"] != 1 or binding != expected
                            or public["message_id"] == original["message_id"]):
                        raise ReviewError("reply_review_revision_unbound")
            # Digest-only host bookkeeping is not part of the model packet or
            # public receipt. A revision is not a factual approval assertion.
            summary = {key: value for key, value in state.items()
                       if key not in ("draft_digest", "accepted_digest", "draft_sequence", "accepted_sequence")}
            summary["final_text_source"] = "same_native_main_assistant_public_message"
            if state.get("decision") == "keep_original":
                original = self._original(state)
                if public["text"] != self.keep_control:
                    raise ReviewError("reply_review_control_unbound")
                summary["final_text_source"] = "original_public_draft_selected_by_native_decision"
                return original["text"], summary
            return public["text"], summary

    def terminal_snapshot(self, native_result, error):
        """Retain private state and exact failed predicates without reply text."""
        with self.adapter.lock:
            row = self.adapter.db.execute("SELECT receipt_json FROM claude_turns "
                "WHERE session_id=? AND client_message_id=?", (self.session, self.turn)).fetchone()
            report = json.loads(row[0] or "{}") if row else {}
            state = copy.deepcopy(report.get("reply_review_hook") or self.initial_state())
            public = self.public_message
            state["_terminal_binding"] = {"version": "companion-review-terminal-binding/v1", "error": error,
                "state_accepted": state.get("state") == "accepted", "public_present": public is not None,
                "sequence_matches": public is not None and state.get("accepted_sequence") == public["sequence"],
                "digest_matches": public is not None and state.get("accepted_digest") == _digest(public["text"]),
                "result_projection_matches": public is not None and isinstance(native_result, str)
                    and native_result in public["projections"],
                "public_message_id": public.get("message_id") if public else None,
                "public_sha256": _digest(public["text"]) if public else None}
            return state

    def record_hook_error(self, code):
        with self.adapter.lock:
            report, state = self._load()
            state["_hook_failure"] = {"stage": "stop_callback", "error": code,
                                      "state": state.get("state")}
            self._save(report, state)

    def verify_final(self, content):
        return self.accepted_final(content)[1]



def _show_review(controller):
    """Display only: the App's companion status says 检查 while a finished draft is reviewed."""
    try:
        from .turn_phase import set_phase
        adapter = controller.adapter
        set_phase(adapter._notify, adapter.active.get((controller.session, controller.turn)), "review")
    except Exception:
        pass


def handle_stop(relay, handler):
    from chat_tools.description_relay import RelayError
    identifier = None
    controller = None
    handler.close_connection = True
    try:
        if handler.command != "POST":
            raise RelayError("reply_review_method_not_allowed", 405)
        relay._request(handler)  # Existing loopback/Host/Origin/per-turn bearer checks.
        body = relay._body(handler, time.monotonic() + 2.0)
        if (not isinstance(body, dict) or set(body) != {"jsonrpc", "id", "method", "params"}
                or body.get("jsonrpc") != "2.0" or body.get("id") != "chat-review-stop"
                or body.get("method") != HOOK_METHOD):
            raise RelayError("reply_review_hook_invalid")
        identifier = body["id"]
        controller = relay.row.get("_reply_review")
        if controller is None or controller.native_id != relay.native["native_id"]:
            raise RelayError("reply_review_hook_unbound", 403)
        _show_review(controller)
        decision = controller.decide(body["params"])
        relay._reply(handler, 200, {"jsonrpc": "2.0", "id": identifier, "result": decision})
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as error:
        code = str(error) if isinstance(error, (RelayError, ReviewError)) else "reply_review_hook_unavailable"
        if controller is not None:
            try:
                controller.record_hook_error(code)
            except Exception:
                pass
        try:
            relay._reply(handler, getattr(error, "status", 503), {"jsonrpc": "2.0", "id": identifier,
                "error": {"code": -32001, "message": code}})
        except (BrokenPipeError, ConnectionResetError):
            pass  # The hook process is already gone (e.g. turn cancelled); the error is recorded above.


def native_stop(config_path, native_session, payload, timeout=7.0):
    from chat_tools.description_relay import _RelayTransport
    if (not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop"
            or payload.get("session_id") != native_session
            or type(payload.get("stop_hook_active")) is not bool):
        raise ReviewError("reply_review_native_input_invalid")
    # Deliberately do not read transcript_path, cwd, permission metadata,
    # tool results, hidden reasoning, or any prior message from this envelope.
    content = payload.get("last_assistant_message")
    select_review_targets(content)
    config = json.loads(Path(config_path).read_text())
    route = config["mcpServers"]["context"]
    url = route["url"]
    if not isinstance(url, str) or not url.startswith("http://127.0.0.1:") or not url.endswith("/mcp/context"):
        raise ReviewError("reply_review_native_route_invalid")
    token = route["headers"]["Authorization"]
    request = {"jsonrpc": "2.0", "id": "chat-review-stop", "method": HOOK_METHOD,
               "params": {"session_id": native_session, "stop_hook_active": payload["stop_hook_active"],
                          "last_assistant_message": content}}
    if timeout not in (7.0, 29.0):
        raise ReviewError("reply_review_native_deadline_invalid")
    carrier = _RelayTransport(url[:-len("/mcp/context")] + HOOK_PATH, time.monotonic() + timeout)
    carrier.response_limit = 28000 if timeout == 29.0 else 20000
    outcome = []
    def run():
        try:
            outcome.append(carrier.post(request, {"Authorization": token}))
        except Exception as error:
            outcome.append(error)
    worker = threading.Thread(target=run, name="native-stop-review", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        carrier.cancel()
        raise ReviewError("reply_review_native_deadline")
    if not outcome or isinstance(outcome[0], Exception) or outcome[0][0] != 200:
        raise ReviewError("reply_review_native_unavailable")
    response = outcome[0][1]
    if response.get("error") or not isinstance(response.get("result"), dict):
        raise ReviewError("reply_review_native_invalid_result")
    decision = response["result"]
    if decision and (set(decision) != {"decision", "reason"} or decision["decision"] != "block"
                     or not isinstance(decision["reason"], str) or not decision["reason"]):
        raise ReviewError("reply_review_native_invalid_result")
    return decision


def native_stop_stdout(decision):
    """Render the already validated reply without changing host state.

    Installed Claude 2.1.284 continues Stop on additionalContext through the
    same loop as block/reason, but only inserts the material once. Its 10,000
    JS-character cap and reminder-tag escaping must not silently alter proof.
    Preserve the original block transport whenever lossless delivery is not
    guaranteed. Old native transcripts are never rewritten.
    """
    if (not isinstance(decision, dict) or set(decision) != {"decision", "reason"}
            or decision.get("decision") != "block" or not isinstance(decision.get("reason"), str)
            or not decision["reason"]):
        return decision
    reason = decision["reason"]
    try:
        units = len(reason.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        return decision
    if (units > 10000 or re.search(r"</?system-reminder\b", reason, re.I)
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", reason)):
        return decision
    return {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": reason}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-config", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--semantic-review", action="store_true")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(1048577 + 65536)
        if len(raw) > 1048576 + 65536:
            raise ReviewError("reply_review_native_input_too_large")
        result = native_stop(args.mcp_config, args.session, json.loads(raw), 29.0 if args.semantic_review else 7.0)
        print(json.dumps(native_stop_stdout(result), ensure_ascii=False))
    except Exception:
        # A missing/failed callback is NOT accepted by the host's final guard.
        # Keep credentials/input/error payloads out of native and user output.
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
