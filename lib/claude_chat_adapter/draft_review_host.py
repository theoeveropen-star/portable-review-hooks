"""Authenticated per-turn source projection and the R3/native protocol bridge.

This module cannot ingest a user turn or publish a reply. The native Stop state
machine owns its durable attempt before calling the reviewer or history RPC.
Only a pinned, host-owned rollout file selects a chat session. Brain owns
the independent reviewer provider/model and its credential.
"""
import copy
from chat_tools.runtime_capabilities import capability_evidence, capability_for_claim
from chat_tools.review_diagnostics import safe_plan_diagnostics
from .persona_config import USER, PRONOUN
import hashlib
import os
import json
import socket
import re
from pathlib import Path
import threading
import time

from chat_tools.reply_review import (ReviewClient, ReviewError, REVIEW_VERSION,
                                    unavailable_review, draft_id)
from .draft_review_contract import (DRAFT_REVIEW_LIMITS, DRAFT_REVIEW_VERSION,
    ONE_REVISION_INSTRUCTION, build_draft_review_input, DraftReviewContractError,
    validate_draft_review_plan, build_review_work)


CONFIG_VERSION = "companion-draft-review-rollout/v1"
# Default: every owner session the bridge signs (prefix entry). Override with
# COMPANION_DRAFT_REVIEW_ROLLOUT_FILE (same format) to narrow or disable review.
CONFIG_PATH = Path(os.environ.get("COMPANION_DRAFT_REVIEW_ROLLOUT_FILE")
                   or Path(__file__).with_name("draft_review_rollout.json"))
REVIEW_SECONDS = 20.0
CLASSIFICATION_VERSION = "companion-chat-draft-classify/v1"
CLASSIFICATION_TIMEOUT = 24.0
SEMANTIC_HISTORY_DEADLINE_MS = 8000
SEMANTIC_HISTORY_RPC_SECONDS = 10.0


def failure_diagnostic(error, stage="brain_dependency"):
    """Only local codes and numeric status escape; never exception/provider text."""
    known = {
        "semantic_classifier_deadline", "reply_review_deadline",
        "semantic_classifier_unavailable", "reply_review_unavailable",
        "semantic_classifier_capability_missing", "semantic_history_capability_missing",
        "semantic_classifier_draft_binding_mismatch", "semantic_classifier_host_binding_mismatch",
        "semantic_classifier_input_binding_mismatch", "semantic_classifier_contract_mismatch",
        "semantic_history_contract_mismatch", "reply_review_contract_mismatch",
        "reply_review_binding_mismatch", "reply_review_invalid_response", "reply_review_rejected",
    }
    code = str(error) if isinstance(error, ReviewError) and str(error) in known else None
    transport_codes = {
        "chat_tool_upstream_timeout": ("semantic_review_timeout", "timeout"),
        "chat_tool_upstream_invalid_response": ("semantic_review_malformed_response", "malformed"),
        "chat_tool_upstream_redirect_refused": ("semantic_review_redirect_refused", "transport"),
        "chat_tool_upstream_unavailable": ("semantic_review_transport_error", "transport"),
    }
    transport = transport_codes.get(getattr(error, "code", None)) if isinstance(getattr(error, "code", None), str) else None
    status = getattr(error, "http_status", getattr(error, "status", getattr(error, "code", None)))
    status = status if type(status) is int and 100 <= status <= 599 else None
    if transport:
        code, category = transport
    elif isinstance(error, (TimeoutError, socket.timeout)) or code and code.endswith("_deadline"):
        category, code = "timeout", code or "semantic_review_timeout"
    elif isinstance(error, (json.JSONDecodeError, UnicodeError)):
        category, code = "malformed", "semantic_review_malformed_response"
    elif status is not None:
        category, code = "http_rejected", code or "semantic_review_http_rejected"
    elif code and any(part in code for part in ("binding", "contract", "invalid_response")):
        category = "contract_mismatch"
    elif isinstance(error, OSError):
        category, code = "transport", code or "semantic_review_transport_error"
    else:
        category, code = "unavailable", code or "semantic_reviewer_unavailable"
    metadata = {"error_category": category, "failure_stage": stage, "completion_unknown": True}
    if status is not None:
        metadata["http_status"] = status
    return {"error": code, "metadata": metadata}


def safe_review_error(error, default="semantic_reviewer_unavailable"):
    """Only bounded machine codes cross the public diagnostic boundary."""
    value = ("draft_review_" if isinstance(error, DraftReviewContractError) else "") + str(error)
    return value if re.fullmatch(r"(?:semantic_|reply_review_|reviewer_|draft_review_|history_)[a-z0-9_]{1,96}", value) else default


def review_failure(error, *, stage="brain_dependency", attempts=None, completion_unknown=True):
    code = safe_review_error(error)
    category = ("timeout" if "deadline" in code or "timeout" in code else
                "malformed" if "json" in code else
                "contract" if any(x in code for x in ("invalid", "mismatch", "contract")) else
                "http" if "http_" in code else "unavailable")
    metadata = {"provider_request_attempts": attempts, "completion_unknown": completion_unknown,
                "error_category": category, "failure_stage": stage}
    status_match = re.search(r"_http_(\d{3})$", code)
    if status_match:
        metadata["http_status"] = int(status_match.group(1))
    return {"status": "unavailable", "plan": None, "work": None, "error": code, "metadata": metadata}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class SemanticHistoryClient(ReviewClient):
    """Keep the common client untouched; extend only this private adapter."""
    semantic_capable = False
    classifier_capable = False
    semantic_history_timeout = 6.0

    def capabilities(self):
        with self._cap_lock:
            if time.monotonic() < self._cap_until:
                return self._capable
            try:
                value = self._rpc({"mode": "capabilities"}, min(2.0, self.timeout))
                self._capable = (value.get("status") == "ok" and value.get("mode") == "capabilities"
                    and value.get("max_reviews_per_turn") == 1 and value.get("max_revisions") == 1
                    and value.get("review_deadline_ms") == 4500)
                self.semantic_capable = self._capable and value.get("semantic_queries") is True and value.get(
                    "semantic_query_contract") == {"version": "host-semantic-history/v1",
                        "max_queries": 4, "max_anchor_bytes": 4096, "max_query_bytes": 768}
                # Negotiate only the history RPC; legacy review and classifier
                # deadlines keep their existing independent contracts.
                history_ms = value.get("semantic_history_deadline_ms", 4500)
                self.semantic_capable = self.semantic_capable and type(history_ms) is int and history_ms in (4500, SEMANTIC_HISTORY_DEADLINE_MS)
                self.semantic_history_timeout = SEMANTIC_HISTORY_RPC_SECONDS if history_ms == SEMANTIC_HISTORY_DEADLINE_MS else self.timeout
                self.classifier_capable = self.semantic_capable and value.get("semantic_classifier") == {
                    "version": CLASSIFICATION_VERSION, "max_classifications": 1,
                    "review_input_version": DRAFT_REVIEW_VERSION, "deadline_ms": 20000}
            except Exception:
                self._capable = self.semantic_capable = self.classifier_capable = False
            self._cap_until = time.monotonic() + (60.0 if self._capable else 5.0)
            return self._capable

    def semantic_review(self, session, turn, identifier, content, queries):
        if not self.semantic_capable:
            raise ReviewError("semantic_history_capability_missing")
        if (not isinstance(queries, list) or not 1 <= len(queries) <= 4
                or any(not isinstance(q, dict) or set(q) != {"text", "kind", "query"}
                    or q["kind"] != "uncertain" or not isinstance(q["text"], str)
                    or not q["text"].strip() or len(q["text"].encode()) > 4096
                    or q["text"] not in content or not isinstance(q["query"], str)
                    or not q["query"].strip() or len(q["query"].encode()) > 768 for q in queries)):
            raise ReviewError("semantic_history_queries_invalid")
        value = self._rpc({"session_id": session, "turn_id": turn, "draft_id": identifier,
                           "content": content, "review_queries": copy.deepcopy(queries)}, self.semantic_history_timeout)
        review = value.get("review")
        if (value.get("status") not in ("ok", "unavailable") or value.get("turn_id") != turn
                or value.get("draft_id") != identifier or not isinstance(review, dict)
                or review.get("mode") not in ("reviewed", "revision_no_retrieval", "unavailable")
                or review.get("approval_provided") is not False
                or not isinstance(review.get("evidence"), list)):
            raise ReviewError("semantic_history_contract_mismatch")
        return value

    def classify_draft(self, session, turn, identifier, content, review_input):
        if not self.classifier_capable:
            raise ReviewError("semantic_classifier_capability_missing")
        # Validate and snapshot before crossing the existing authenticated RPC.
        validate_draft_review_plan({"route": "pass", "issues": []}, review_input)
        if review_input["draft"]["content"] != content:
            raise ReviewError("semantic_classifier_draft_binding_mismatch")
        arguments = {"mode": "classify", "session_id": session, "turn_id": turn,
            "draft_id": identifier, "content": content, "review_input": copy.deepcopy(review_input)}
        exact = {}

        def capture(request, timeout):
            # Reuse the existing ReviewClient envelope/binding validation with
            # a per-call transport closure; never mutate the shared client's
            # transport or let one turn read another turn's last response.
            if self.transport is not None:
                response = self.transport(request, timeout)
            else:
                from chat_tools.description_relay import _RelayTransport
                carrier = _RelayTransport(self.url, time.monotonic() + timeout)
                carrier.response_limit = 65536
                outcome = []
                def send():
                    try:
                        outcome.append(carrier.post(request, {"Authorization": "Bearer " + self.token}))
                    except Exception as error:
                        outcome.append(error)
                worker = threading.Thread(target=send, name="draft-classify-rpc", daemon=True)
                worker.start()
                worker.join(timeout)
                if worker.is_alive():
                    carrier.cancel()
                    raise ReviewError("semantic_classifier_deadline")
                if outcome and isinstance(outcome[0], Exception):
                    raise outcome[0]
                if outcome and outcome[0][0] != 200:
                    error = ReviewError("semantic_classifier_unavailable")
                    error.http_status = outcome[0][0]
                    raise error
                if not outcome:
                    raise ReviewError("semantic_classifier_unavailable")
                response = outcome[0][1]
            if isinstance(response, dict) and isinstance(response.get("result"), dict):
                meta = response["result"].get("_meta")
                if isinstance(meta, dict):
                    exact["machine"] = copy.deepcopy(meta.get("companion/draft-classification"))
            return response

        checked = ReviewClient(self.url, self.token, transport=capture)
        value = checked._rpc(arguments, CLASSIFICATION_TIMEOUT)
        machine = exact.get("machine")
        input_sha256 = hashlib.sha256(_json(review_input).encode()).hexdigest()
        if (not isinstance(machine, dict)
                or set(machine) != {"version", "turn_id", "draft_id", "input_sha256", "result"}
                or machine.get("version") != CLASSIFICATION_VERSION
                or machine.get("turn_id") != turn or machine.get("draft_id") != identifier
                or machine.get("input_sha256") != input_sha256 or len(_json(machine).encode()) > 32768):
            raise ReviewError("semantic_classifier_host_binding_mismatch")
        result = machine.get("result")
        public = value.get("result")
        if (value.get("mode") != "classify" or value.get("classification_version") != CLASSIFICATION_VERSION
                or value.get("status") not in ("ok", "unavailable")
                or value.get("turn_id") != turn or value.get("draft_id") != identifier
                or not isinstance(result, dict) or result.get("status") not in ("ok", "unavailable", "rejected")
                or not isinstance(public, dict) or public.get("material_channel") != "host_meta"
                or public.get("status") != result.get("status")
                or (value.get("status") == "ok") != (result.get("status") == "ok")
                or result.get("approval_provided") is not False or result.get("storage_performed") is not False
                or result.get("delivery_confirmed") is not False):
            raise ReviewError("semantic_classifier_contract_mismatch")
        result = copy.deepcopy(result)
        if result["status"] == "ok":
            if (result.get("metadata", {}).get("input_sha256")
                    != input_sha256):
                raise ReviewError("semantic_classifier_input_binding_mismatch")
            plan = validate_draft_review_plan(result.get("plan"), review_input)
            result["plan"] = plan
            result["work"] = build_review_work(plan, review_input)
        elif result.get("plan") is not None or result.get("work") is not None:
            raise ReviewError("semantic_classifier_contract_mismatch")
        return result


def recent_canonical_context(adapter, row, native):
    """Current source ledger, same lineage, completed before this user arrived.

    No transcript, pending draft, summary or future turn can become a source.
    Whole messages are retained; the contract reports later size omissions.
    """
    with adapter.lock:
        lineage = adapter.slim.context_native_ids(native["native_id"])
        if not lineage:
            return []
        prior = adapter.db.execute(
            "SELECT user_text,assistant_text FROM claude_turns "
            "WHERE session_id=? AND native_id IN (" + ",".join("?" for _ in lineage) + ") "
            "AND status='completed' AND completed_at<=? AND client_message_id!=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 2",
            (row["session_id"], *lineage, row["created_at"], row["client_message_id"])).fetchall()
    messages = []
    for item in reversed(prior):
        for role, content in (("user", item["user_text"]), ("assistant", item["assistant_text"])):
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
    return messages


def opening_canonical_context(adapter, row):
    """Only typed sources from actual delivered packets in this native lineage."""
    from chat_tools.review_evidence import evidence_context, audit_evidence
    with adapter.lock:
        source = adapter.db.execute(
            "SELECT native_id FROM claude_turns WHERE session_id=? AND client_message_id=?",
            (row["session_id"], row["client_message_id"])).fetchone()
        if source is None:
            raise ReviewError("semantic_review_current_source_unbound")
        lineage = adapter.slim.context_native_ids(source[0])
        if not lineage:
            return []
        previous = adapter.db.execute(
            "SELECT c.metadata FROM claude_context c JOIN claude_turns t USING(session_id,client_message_id) "
            "WHERE t.session_id=? AND t.native_id IN (" + ",".join("?" for _ in lineage) + ") "
            "AND t.status='completed' AND t.completed_at<=? AND c.delivered=1 "
            "AND t.client_message_id!=? ORDER BY t.created_at DESC,t.rowid DESC LIMIT 20",
            (row["session_id"], *lineage, row["created_at"], row["client_message_id"])).fetchall()
        current = adapter.db.execute(
            "SELECT metadata FROM claude_context WHERE session_id=? AND client_message_id=?",
            (row["session_id"], row["client_message_id"])).fetchone()
        events = adapter.db.execute(
            "SELECT client_message_id,completed_at,receipt_json FROM claude_turns "
            "WHERE session_id=? AND native_id IN (" + ",".join("?" for _ in lineage) + ") "
            "AND status='completed' AND completed_at<=? AND client_message_id!=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 6",
            (row["session_id"], *lineage, row["created_at"], row["client_message_id"])).fetchall()
    result = []
    for packet in [*reversed(previous), *([current] if current else [])]:
        result.extend(evidence_context(json.loads(packet[0]).get("review_evidence", [])))
    records = []
    for turn, completed, raw in reversed(events):
        state = json.loads(raw or "{}").get("reply_review_hook", {})
        if state.get("enabled") is True:
            records.append({"turn_id": turn, "completed_at": completed,
                **{key: state[key] for key in ("review_attempts", "revision_count", "final_review", "review_status") if key in state}})
    return result + audit_evidence(records, "up to six completed turns in this current native lineage before current ingress")


# "prefix:<p>" selects every session the owner bridge signs under <p>, so a new
# chat (new client session id -> new HMAC) keeps the reviewer. The prefix must
# be long enough that it can never be a bare profile or empty match.
ROLLOUT_PREFIX = "prefix:"
ROLLOUT_PREFIX_MIN = 24


def _session_selected(session_id, sessions):
    for entry in sessions:
        if entry.startswith(ROLLOUT_PREFIX):
            if session_id.startswith(entry[len(ROLLOUT_PREFIX):]):
                return True
        elif session_id == entry:
            return True
    return False


def create_turn_reviewer(adapter, row, window, client):
    if not CONFIG_PATH.exists():
        return None
    try:
        raw = CONFIG_PATH.read_bytes()
        if len(raw) > 8192:
            raise ValueError()
        cfg = json.loads(raw)
        if (not isinstance(cfg, dict) or set(cfg) !=
                {"version", "enabled", "sessions", "classifier_version"}
                or cfg["version"] != CONFIG_VERSION or type(cfg["enabled"]) is not bool
                or cfg["classifier_version"] != CLASSIFICATION_VERSION
                or not isinstance(cfg["sessions"], list) or len(cfg["sessions"]) > 16
                or any(not isinstance(s, str) or not s or len(s) > 256 for s in cfg["sessions"])
                or len(cfg["sessions"]) != len(set(cfg["sessions"]))
                or any(s.startswith(ROLLOUT_PREFIX) and len(s) - len(ROLLOUT_PREFIX) < ROLLOUT_PREFIX_MIN
                       for s in cfg["sessions"])
                or cfg["enabled"] and not cfg["sessions"]):
            raise ValueError()
        if not cfg["enabled"] or not _session_selected(row["session_id"], cfg["sessions"]):
            return None
        if not getattr(client, "semantic_capable", False) or not getattr(client, "classifier_capable", False):
            raise ReviewError("semantic_classifier_capability_missing")
        # The actual Stop controller supplies the same logical window and
        # context turn identity. Never read the main-chat API settings or keys.
        with adapter.lock:
            active = adapter.active.get((row["session_id"], row["client_message_id"]))
            if active is None or active["cancel"].is_set():
                raise ReviewError("semantic_review_turn_unbound")
            cancel = active["cancel"]
        return BrainTurnReviewer(client, window, row["client_message_id"], cancel, hashlib.sha256(raw).hexdigest(),
            capability_snapshot=copy.deepcopy(getattr(adapter, "runtime_capability_snapshot", None)),
            capability_session_id=row["session_id"],
            capability_now_ms=getattr(adapter, "runtime_capability_now_ms", None))
    except ReviewError:
        raise
    except Exception:
        raise ReviewError("semantic_review_configuration_invalid") from None

def history_observation(value):
    """Content-free bounded timing; absence stays unknown, never means zero."""
    source = value if isinstance(value, dict) else {}
    result = {"version": "companion-history-observation/v1"}
    if source.get("observation_status") == "binding_rejected":
        result["observation_status"] = "binding_rejected"
    for key in ("elapsed_ms", "rpc_elapsed_ms"):
        number = source.get(key)
        if type(number) in (int, float) and 0 <= number < 100000000:
            result[key] = round(number, 3)
    deadline = source.get("deadline")
    if isinstance(deadline, dict):
        bounded = {key: deadline[key] for key in ("total_ms", "initial_phase_ms", "followup_reserved_ms")
                   if type(deadline.get(key)) in (int, float) and 0 <= deadline[key] <= SEMANTIC_HISTORY_DEADLINE_MS}
        if bounded:
            result["deadline"] = bounded
    lanes = source.get("lanes")
    if isinstance(lanes, list):
        result["lanes"] = []
        for lane in lanes[:12]:
            if not isinstance(lane, dict) or lane.get("lane") not in (
                    "memory", "archive", "archive_followup", "archive_later_state"):
                continue
            item = {"lane": lane["lane"]}
            for key, allowed in (("status", {"ok", "partial", "no_match", "unavailable"}),
                                 ("reason", {"deadline", "invalid_response", "tool_error", "transport_error"}),
                                 ("deadline_scope", {"initial_phase_with_followup_time_reserved", "shared_total_deadline"})):
                if isinstance(lane.get(key), str) and lane[key] in allowed:
                    item[key] = lane[key]
            result["lanes"].append(item)
        omitted = source.get("lanes_omitted", 0)
        omitted = omitted if type(omitted) is int and 0 <= omitted <= 1000000 else 0
        if len(lanes) > 12 or omitted:
            result["lanes_omitted"] = min(1000000, max(0, len(lanes) - 12) + omitted)
    return result


def revision_basis(work, receipt):
    """Missing history is a support gap, not a factual correction instruction."""
    historical = receipt.get("review", {})
    issues = work.get("issues", [])
    current_ids = {item.get("id") for item in work.get("current_evidence", []) if isinstance(item, dict)}
    current = [issue for issue in issues
               if issue.get("kind") in ("current_conflict", "current_repeat", "current_unsupported")
               and any(eid in current_ids for eid in issue.get("current_evidence_ids", []))]
    history = [issue for issue in issues if issue.get("kind") in ("history_dependency", "history_request")]
    available = receipt.get("status") == "ok" and bool(historical.get("evidence"))
    scoped = historical.get("blocked_target_coverage") or {}
    scoped_obligation = (scoped.get("scope") == "current_user_bound_query_targets"
        and any(isinstance(item, dict) and item.get("decision") == "forbidden"
                for item in scoped.get("blocked_targets", []))
        and "user_scope_forbidden" in historical.get("warnings", []))
    user_forbidden = (receipt.get("status") == "ok" and historical.get("retrieval_status") == "not_run"
        and historical.get("retrieval_started") is False and "user_forbidden" in historical.get("warnings", []))
    scope_obligation = bool(user_forbidden or scoped_obligation)
    target_coverage = historical.get("target_coverage")
    if isinstance(target_coverage, list):
        from prefetch_reuse import key
        coverage = {key(x["query"]): x["status"] for x in target_coverage
                    if isinstance(x, dict) and isinstance(x.get("query"), str) and x.get("status") in ("candidate", "no_match", "unavailable")}
        covered = [issue for issue in history if coverage.get(key(issue.get("query", ""))) == "candidate"]
        unresolved = [issue for issue in history if coverage.get(key(issue.get("query", ""))) != "candidate"]
    else:
        covered = history if available else []
        unresolved = [] if available else history
    actionable = [issue for issue in issues if issue in current or (issue in covered and available) or (issue in history and scope_obligation)]
    gap = bool(unresolved and not scope_obligation)
    return {"issues": actionable, "current_issue_count": len(current),
            "history_evidence_available": available, "scope_obligation": scope_obligation,
            "support_gap": gap, "unresolved_history_issue_count": len(unresolved) if gap else 0,
            "needs_revision": work.get("route") in ("current_repair", "history_review")
                and bool(actionable or scope_obligation)}


class TurnReviewer:
    def __init__(self, cancel, config_sha256):
        self.cancel, self.config_sha256 = cancel, config_sha256
        self.result = None
        self.input = None
        self.history_attempts = 0
        self.history_observation = None


    def history(self, client, window, turn, identifier, content):
        work = (self.result or {}).get("work")
        if not work or work.get("route") != "history_review" or self.history_attempts:
            raise ReviewError("semantic_history_attempt_invalid")
        if self.cancel.is_set():
            raise ReviewError("reply_review_turn_inactive")
        self.history_attempts = 1
        started = time.monotonic()
        try:
            receipt = client.semantic_review(window, turn, identifier, content, work["history_targets"])
        except Exception as error:
            diagnostic = failure_diagnostic(error)
            receipt = unavailable_review(turn, identifier, diagnostic["error"], diagnostic=diagnostic)
        self.history_observation = history_observation(receipt.get("review", {}).get("retrieval_observation"))
        self.history_observation["rpc_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        return receipt

    def summary(self):
        result = self.result or {}
        metadata = result.get("metadata") or {}
        # Allowlist metadata even if a future provider client returns more.
        safe = {k: metadata[k] for k in ("protocol", "model_requested", "model_returned", "elapsed_ms",
            "provider_request_attempts", "request_bytes", "response_bytes", "request_sha256", "response_sha256",
            "completion_unknown", "thinking_mode", "reasoning_effort",
            "error_category", "http_status", "failure_stage") if k in metadata}
        safe.update(safe_plan_diagnostics(result))
        return {"version": DRAFT_REVIEW_VERSION, "status": result.get("status", "unavailable"),
                "route": (result.get("plan") or {}).get("route"),
                "error": result.get("error"), "metadata": safe, "usage": result.get("usage"),
                "history_attempts": self.history_attempts, "config_sha256": self.config_sha256,
                **({"fact_questions": len(fact_question_queries(result))} if fact_question_queries(result) else {}),
                **({"retrieval_observation": self.history_observation} if self.history_observation is not None else {}),
                "input_coverage": (self.input or {}).get("coverage"),
                "recall_scope": {"scope": (result.get("plan") or {}).get("recall_scope", "shared") if result.get("status") == "ok" else "shared",
                    "content_sha256": hashlib.sha256(self.input["draft"]["content"].encode()).hexdigest() if self.input else None},
                "approval_provided": False}

    def instruction(self, receipt):
        result = self.result or {}
        work = result.get("work") or {}
        historical = receipt.get("review", {})
        basis = revision_basis(work, receipt)
        history = copy.deepcopy({k: historical[k] for k in ("mode", "retrieval_status", "retrieval_started",
            "evidence", "guidance", "warnings", "coverage", "target_coverage", "blocked_target_coverage", "query_execution", "display_partial", "diagnostic") if k in historical})
        if historical.get("mode") == "unavailable":
            history["guidance"] = []
        packet = {"route": work.get("route", "reviewer_unavailable"),
                  "issues": copy.deepcopy(basis["issues"]),
                  "current_evidence": copy.deepcopy(work.get("current_evidence", [])),
                  "history": history, "approval_provided": False}
        if basis["support_gap"]:
            packet["support_gap"] = {"status": "unresolved", "history_status": history.get("retrieval_status", "unavailable"),
                "issue_count": basis["unresolved_history_issue_count"], "revision_required": False,
                "scope": "History not established; missing evidence alone does not require changing the draft or adding uncertainty."}
        # The historical reply is already bounded by Brain. If a future source
        # grows, omit complete records with explicit coverage; never splice text
        # into invented evidence or claim that omitted source text was delivered.
        history.pop("coverage", None)
        history.pop("guidance", None)
        selected_current = packet["current_evidence"]
        selected_history = history.get("evidence", [])
        if isinstance(historical.get("reverse_recall"), dict):
            packet["reverse_recall"] = copy.deepcopy(historical["reverse_recall"])
        if isinstance(historical.get("fact_questions"), dict):
            packet["fact_questions"] = copy.deepcopy(historical["fact_questions"])
        packet["current_evidence"] = []; history["evidence"] = []
        for kind, records in (("current", selected_current), ("history", selected_history)):
            destination = packet["current_evidence"] if kind == "current" else history["evidence"]
            for record in records:
                destination.append(record)
                if len(_json(packet).encode()) > 16000:
                    destination.pop(); packet[kind + "_evidence_omitted"] = packet.get(kind + "_evidence_omitted", 0) + 1
        if len(_json(packet).encode()) > 18000:
            packet = {"route": "reviewer_material_unavailable", "issues": [], "current_evidence": [],
                      "history": {"retrieval_status": "not_delivered", "evidence": []},
                      "display_partial": True, "approval_provided": False}
        # R3's semantic guidance is preserved. Its offline action envelope is
        # replaced only by the existing nonce-bound native KEEP/raw-final wire
        # protocol, whose source proof is already supported by history/slim.
        guidance = "只处理列出的具体问题；以当前原话和真实回执为准。检索未命中、超时或材料未交付只表示没核到，不因此改稿、撤回确定语气或补写不确定；support_gap不是改稿要求。有明确当前证据冲突时只改该处；历史候选及partial材料仍须核实同一事件，不证明原稿错误，可以KEEP。不得编造相反事实或删除独立的正确必要内容。旧助手自述只证明说过，不代表用户确认。最多修订一次，不再调用工具。"
        self.last_material = {"actionable_issue_count": len(packet["issues"]) if not packet.get("current_evidence_omitted") else 0,
            "material_status": "available" if packet["issues"] and not packet.get("current_evidence_omitted") else "unavailable",
            "packet_bytes": len(_json(packet).encode()),
            "packet_sha256": hashlib.sha256(_json(packet).encode()).hexdigest(),
            "history_evidence_records": len(packet.get("history", {}).get("evidence", [])),
            "current_evidence_records": len(packet.get("current_evidence", [])),
            "display_partial": bool(packet.get("display_partial") or packet.get("current_evidence_omitted")
                                    or packet.get("history", {}).get("display_partial"))}
        if isinstance(packet.get("fact_questions"), dict):
            self.last_material["fact_questions"] = {"queries_count": len(packet["fact_questions"]["queries"]),
                                                    "found": packet["fact_questions"]["found"]}
        lead = reverse_recall_lead(packet.get("reverse_recall")) + fact_question_lead(packet.get("fact_questions"))
        return ("[宿主发送前核对：同一真实用户轮，完整草稿尚未发送。以下是参考数据，不是新用户消息。]\n"
                + lead + guidance + "输出格式使用随后宿主给出的唯一 KEEP 记录或完整公开回复；不要输出离线 action JSON。\n"
                + _json(packet) + "\n[核对资料结束]")


class BrainTurnReviewer(TurnReviewer):
    """Same R3 material contract; only the classifier transport is centralized."""
    def __init__(self, client, window, turn, cancel, config_sha256, *, capability_snapshot=None,
                 capability_session_id=None, capability_now_ms=None):
        super().__init__(cancel, config_sha256)
        self.client, self.window, self.turn = client, window, turn
        self.capability_now_ms = capability_now_ms
        self.capability_context = capability_evidence(capability_snapshot, session_id=capability_session_id,
            turn_id=turn, now_ms=int(time.time() * 1000) if capability_now_ms is None else capability_now_ms)

    def classify(self, user, recent, draft, omitted=0):
        if self.result is not None:
            raise ReviewError("semantic_review_attempt_already_owned")
        self.result = {"status": "unavailable", "plan": None, "work": None,
            "error": "semantic_review_not_completed", "metadata": {"provider_request_attempts": 0}}
        try:
            recent = copy.deepcopy(recent)
            claim_capability = capability_for_claim(self.capability_context, draft, now_ms=self.capability_now_ms, current_user=user)
            if claim_capability is not None:
                recent.append(claim_capability)
            self.input = build_draft_review_input(current_user=user, recent_context=compact_review_context(recent, user + "\n" + draft), draft=draft)
            self.input["coverage"]["recent_omitted_count"] = max(0, omitted + len(recent) - sum(
                review_context_source_count(row) for row in self.input["recent_context"]))
        except DraftReviewContractError as error:
            self.result = review_failure(error, stage="input_validation",
                                         attempts=0, completion_unknown=False)
            return self.result
        if self.cancel.is_set():
            self.result["error"] = "reply_review_turn_inactive"
            return self.result
        identifier = draft_id(self.window, self.turn, draft)
        try:
            self.result = self.client.classify_draft(self.window, self.turn, identifier, draft, self.input)
        except Exception as error:
            self.result = review_failure(error, stage="plan_validation" if isinstance(error, DraftReviewContractError)
                                         else "brain_dependency", completion_unknown=not isinstance(error, DraftReviewContractError))
        from .review_name_guard import apply_guard
        self.result = apply_guard(self.result, draft, user)
        if self.result.get("status") == "ok":
            self.result["work"] = build_review_work(self.result["plan"], self.input)
        if self.cancel.is_set():
            self.result = {"status": "unavailable", "plan": None, "work": None,
                "error": "reply_review_turn_inactive", "metadata": copy.deepcopy(self.result.get("metadata", {}))}
        return self.result



from chat_tools.review_evidence import compact_review_context, review_context_source_count


REVERSE_RECALL_RECORDS = 2
REVERSE_RECALL_TEXT_CHARS = 360


def reverse_recall_queries(result):
    return list(((result or {}).get("name_guard") or {}).get("guess_queries") or [])


def fact_question_queries(result):
    return list(((result or {}).get("name_guard") or {}).get("fact_queries") or [])


def fact_question_attrs(result):
    return list(((result or {}).get("name_guard") or {}).get("fact_attrs") or [])


def fact_rank_words(result):
    from .review_name_guard import FACT_RANK_WORDS
    return [w for attr in fact_question_attrs(result) for w in FACT_RANK_WORDS.get(attr, ())]


def fact_question_found(evidence, fact):
    """Records naming a questioned subject (the query's first word)."""
    heads = [q.split()[0] for q in fact if q.split()]
    return sum(1 for r in evidence if isinstance(r.get("text"), str) and any(h in r["text"] for h in heads))


def guess_only(issues, guess):
    history = [i for i in issues if i.get("kind") in ("history_dependency", "history_request")]
    return bool(history) and all(i.get("query") in guess for i in history)


def cap_reverse_recall(evidence, issues, guess, extra=()):
    """When the only history questions are the host's own guess queries, hand over
    at most two quotes, each cut to a bounded excerpt (marked, never spliced)."""
    if not guess_only(issues, guess):
        return evidence, False
    # Archive search ranks by any shared word, so it is never empty: keep only
    # records that name the guessed subject, most query words first.
    from .review_name_guard import ATTR_NAMES
    terms = [[t for t in q.split() if t] for q in guess]
    heads = [next((t for t in ts if t not in ATTR_NAMES), None) for ts in terms]
    words = {t for ts in terms for t in ts} | set(extra)
    text = lambda r: r.get("text") if isinstance(r.get("text"), str) else ""
    relevant = [r for r in evidence if any(h and h in text(r) for h in heads)]
    relevant = sorted(relevant, key=lambda r: -sum(w in text(r) for w in words))
    kept, capped = [], len(relevant) > REVERSE_RECALL_RECORDS or len(relevant) < len(evidence)
    for record in relevant[:REVERSE_RECALL_RECORDS]:
        record = copy.deepcopy(record)
        text = record.get("text")
        if isinstance(text, str) and len(text) > REVERSE_RECALL_TEXT_CHARS:
            record.update(text=text[:REVERSE_RECALL_TEXT_CHARS], text_state="excerpt", truncated=True, review_excerpt=True)
            capped = True
        for key in ("source_display", "retrieval", "source_window_aliases"):
            record.pop(key, None)
        kept.append(record)
    return kept, capped


def apply_reverse_recall(receipt, result):
    """Filter/cap the host's own guess/fact-question retrieval once, before any revision basis.

    Evidence is filtered only when every history question is a host query; an
    unavailable or unstarted retrieval is left exactly as it was (not "没翻到").
    Fact questions also get a summary when the provider has its own history issues.
    """
    guess = reverse_recall_queries(result)
    fact = fact_question_queries(result)
    host = guess + fact
    work = (result or {}).get("work") or {}
    review = receipt.get("review") if isinstance(receipt, dict) else None
    if (not host or work.get("route") != "history_review" or receipt.get("status") != "ok"
            or not isinstance(review, dict) or review.get("retrieval_started") is not True):
        return receipt
    only = guess_only(work.get("issues", []), host)
    if not only and not fact:
        return receipt
    receipt = copy.deepcopy(receipt)
    if only:
        evidence, capped = cap_reverse_recall(list(review.get("evidence") or []), work.get("issues", []), host,
                                              fact_rank_words(result))
        receipt["review"]["evidence"] = evidence
        if guess:
            receipt["review"]["reverse_recall"] = {"queries": guess, "found": len(evidence), "capped": capped}
    if fact:
        receipt["review"]["fact_questions"] = {"queries": fact, "attrs": fact_question_attrs(result),
            "found": fact_question_found(list(receipt["review"].get("evidence") or []), fact)}
    return receipt


def fact_question_lead(summary):
    """The draft asks her something she already told; the host looked it up."""
    if not summary or not summary.get("found"):
        return ""
    attrs = summary.get("attrs") or []
    asked = "、".join("「" + q + "」" + ("（" + attrs[i] + "）" if i < len(attrs) else "") for i, q in enumerate(summary["queries"]))
    return ("草稿在问" + USER + "一件" + PRONOUN + "早就告诉过你的事，宿主已经按" + asked + "替你翻了，结果在 history.evidence（原话/记忆+来源）。"
            "答案在里面就别再问" + PRONOUN + "：删掉这个问题，或者直接用答案接着说；翻到的不是同一个对象，才照原样问。")


def reverse_recall_lead(summary):
    """One plain line telling the model the host already looked, and what came back."""
    if not summary:
        return ""
    asked = "、".join("「" + q + "」" for q in summary["queries"])
    if summary["found"]:
        return ("草稿里有没核实/猜的地方，宿主已经按" + asked + "替你翻了原话，结果在 history.evidence（原话+时间+来源）；"
                "就用这些原话当场改，不用也不能再调工具。原话不是同一件事就照实说没翻到。")
    return ("草稿里有没核实/猜的地方，宿主已经按" + asked + "替你翻过原话：没翻到。照实说没翻到或问" + USER + "，"
            "不要编，也不要说下一轮去翻。")


def local_receipt(turn, identifier, route, unavailable=False, diagnostic=None):
    if unavailable:
        return unavailable_review(turn, identifier, (diagnostic or {}).get("error") or
                                  "semantic_reviewer_unavailable", diagnostic=diagnostic)
    return {"version": REVIEW_VERSION, "status": "ok", "turn_id": turn, "draft_id": identifier,
            "read_only": True, "storage_performed": False, "delivery_confirmed": False, "approval_provided": False,
            "review": {"mode": "present_skip" if route == "pass" else "current_repair",
                "retrieval_status": "not_run", "retrieval_started": False, "evidence": [],
                "guidance": [], "warnings": [], "approval_provided": False}}
