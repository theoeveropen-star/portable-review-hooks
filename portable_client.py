"""Portable Stop-review dependencies; this module is not the Stop hook or host.

Host-only environment: PORTABLE_REVIEW_MODEL_BASE_URL, PORTABLE_REVIEW_MODEL_NAME,
PORTABLE_REVIEW_MODEL_API_KEY, PORTABLE_RECALL_URL, optional PORTABLE_RECALL_TOKEN
and PORTABLE_REVIEW_THINKING=disabled (omit to leave provider defaults intact).
Remove these variables from the Claude process environment.  The host must own
per-turn attempt reservations, source and final-event binding, and cancellation.

Recall HTTP contract v1 (UTF-8 JSON POST, one request, no redirects):
  request: {version, session_id, turn_id, draft_id, current_user, recall_scope, queries}
  response: {version, session_id, turn_id, draft_id, request_sha256, status,
             evidence}
version is "portable-review-recall/v1". request_sha256 is SHA-256 over the
exact UTF-8 request bytes. status is "ok", "no_match", or "unavailable".
For ok, evidence contains 1..12 complete records, each exactly
{source_id, source_type, text, date}; source_type is user, assistant, tool or
document, date is a source-provided string or null. For no_match/unavailable
evidence is empty. Each text is at most 4096 UTF-8 bytes, whole response at
most 65536 bytes. Never synthesize an excerpt, role, date, or source ID.
The endpoint is a trusted read-only adapter: it must enforce the user's own
authorization/scope and return actual evidence. A matching ID/digest verifies
transport binding, not the truth or provenance of a third-party record.
HTTP is accepted only on a loopback address; remote endpoints require HTTPS.
Unavailability is never interpreted as a successful search with no matches.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import ipaddress
import json
import os
import threading
from urllib.parse import urlsplit

from chat_tools.reply_review import ReviewError, draft_id, unavailable_review
from claude_chat_adapter.draft_review_client import ProviderSnapshot, classify_once
from claude_chat_adapter.draft_review_contract import validate_draft_review_plan, build_review_work

VERSION = "portable-review-recall/v1"
MAX_RESPONSE = 65536


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _text(value, limit, *, empty=False):
    try:
        return isinstance(value, str) and (empty or bool(value.strip())) and len(value.encode("utf-8")) <= limit
    except UnicodeError:
        return False


def _target(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError()
        if p.scheme == "http":
            if p.hostname != "localhost" and not ipaddress.ip_address(p.hostname).is_loopback:
                raise ValueError()
        port = p.port or (443 if p.scheme == "https" else 80)
        return p, port
    except (TypeError, ValueError):
        raise ReviewError("semantic_history_configuration_invalid") from None


def _post(url, token, body, timeout):
    p, port = _target(url)
    if token is not None and (not _text(token, 4096) or any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise ReviewError("semantic_history_configuration_invalid")
    result = []
    holder = {}
    lock = threading.Lock()
    def run():
        conn = None
        try:
            conn = (http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection)(p.hostname, port, timeout=timeout)
            with lock:
                holder["conn"] = conn
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            path = p.path or "/"
            if p.query:
                path += "?" + p.query
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                raise ReviewError("semantic_history_http_rejected")
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ReviewError("semantic_history_contract_mismatch")
            value = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            result.append(value)
        except Exception:
            result.append(ReviewError("semantic_history_unavailable"))
        finally:
            if conn is not None:
                conn.close()
    worker = threading.Thread(target=run, daemon=True, name="portable-recall")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        with lock:
            conn = holder.get("conn")
            if conn is not None:
                conn.close()
        raise ReviewError("semantic_history_deadline")
    if not result or isinstance(result[0], Exception):
        raise ReviewError("semantic_history_unavailable")
    return result[0]


class PortableReviewClient:
    """Interface compatible with SemanticHistoryClient, with no Brain RPC."""
    semantic_capable = True
    classifier_capable = True

    def __init__(self, model_base_url, model_name, model_api_key, recall_url, recall_token=None, *, thinking=None):
        if thinking is not None and thinking != "disabled":
            raise ReviewError("semantic_classifier_configuration_invalid")
        self.provider = ProviderSnapshot(model_api_key, model_base_url, model_name, "openai", thinking=thinking)
        _target(recall_url)
        self.recall_url, self.recall_token = recall_url, recall_token
        self.current_user = None
        self.recall_scope = None
        self.prefetch = None
        self.classifier_saw_prefetch = False

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("PORTABLE_REVIEW_MODEL_BASE_URL"),
                   os.environ.get("PORTABLE_REVIEW_MODEL_NAME"),
                   os.environ.get("PORTABLE_REVIEW_MODEL_API_KEY"),
                   os.environ.get("PORTABLE_RECALL_URL"), os.environ.get("PORTABLE_RECALL_TOKEN"),
                   thinking=os.environ.get("PORTABLE_REVIEW_THINKING"))

    def capabilities(self):
        # Configuration is not a live service health check.
        return True

    @staticmethod
    def _binding(session, turn, identifier, content):
        if (not all(_text(item, 512) for item in (session, turn, identifier))
                or not _text(content, 1048576) or identifier != draft_id(session, turn, content)):
            raise ReviewError("semantic_classifier_draft_binding_mismatch")

    def set_delivered_prefetch(self, value):
        self.prefetch = copy.deepcopy(value)

    def classify_draft(self, session, turn, identifier, content, review_input):
        self._binding(session, turn, identifier, content)
        validate_draft_review_plan({"route": "pass", "issues": []}, review_input)
        if review_input["draft"]["content"] != content:
            raise ReviewError("semantic_classifier_draft_binding_mismatch")
        from prefetch_reuse import in_classifier
        self.classifier_saw_prefetch = bool(self.prefetch and in_classifier(self.prefetch["packet"], review_input))
        result = classify_once(copy.deepcopy(review_input), self.provider)
        if result.get("status") == "ok":
            plan = validate_draft_review_plan(result.get("plan"), review_input)
            result["plan"] = plan
            result["work"] = build_review_work(plan, review_input)
            self.current_user = review_input["current_user"]["content"]
            self.recall_scope = plan.get("recall_scope", "shared")
        elif result.get("plan") is not None or result.get("work") is not None:
            raise ReviewError("semantic_classifier_contract_mismatch")
        return result

    def semantic_review(self, session, turn, identifier, content, queries):
        self._binding(session, turn, identifier, content)
        if (not isinstance(queries, list) or not 1 <= len(queries) <= 4
                or any(not isinstance(q, dict) or set(q) != {"text", "kind", "query"}
                    or q["kind"] != "uncertain" or not _text(q["text"], 4096)
                    or q["text"] not in content or not _text(q["query"], 768) for q in queries)):
            raise ReviewError("semantic_history_queries_invalid")
        if self.current_user is None or self.recall_scope not in ("work", "life", "shared"):
            raise ReviewError("semantic_history_configuration_invalid")
        from prefetch_reuse import key
        entries = self.prefetch["packet"]["entries"] if self.classifier_saw_prefetch else []
        matched = {}  # Per-query coverage; never infer scope or synonymy.
        spellings = {}
        for q in queries:
            spellings.setdefault(key(q["query"]), set()).add(q["query"])
        for q in queries:
            candidates = [e for e in entries if e["scope"] == self.recall_scope and key(e["query"]) == key(q["query"])]
            if len(candidates) == 1 and len(spellings[key(q["query"])]) == 1:
                matched[key(q["query"])] = candidates[0]
        pending = [q for q in queries if key(q["query"]) not in matched]
        reused = [e for e in matched.values() if e["status"] == "ok"]
        request = {"version": VERSION, "session_id": session, "turn_id": turn,
                   "draft_id": identifier, "current_user": self.current_user,
                   "recall_scope": self.recall_scope, "queries": copy.deepcopy(pending)}
        body = _json(request)
        try:
            if not pending:
                value = {**{k: request[k] for k in ("version", "session_id", "turn_id", "draft_id")},
                         "request_sha256": hashlib.sha256(body).hexdigest(),
                         "status": "no_match", "evidence": []}
            else:
                try:
                    value = _post(self.recall_url, self.recall_token, body, 8.0)
                except Exception:
                    value = {**{k: request[k] for k in ("version", "session_id", "turn_id", "draft_id")},
                             "request_sha256": hashlib.sha256(body).hexdigest(),
                             "status": "unavailable", "evidence": []}
            if (not isinstance(value, dict) or set(value) != {"version", "session_id", "turn_id", "draft_id", "request_sha256", "status", "evidence"}
                    or value["version"] != VERSION or value["session_id"] != session or value["turn_id"] != turn
                    or value["draft_id"] != identifier or value["request_sha256"] != hashlib.sha256(body).hexdigest()
                    or value["status"] not in ("ok", "no_match", "unavailable") or not isinstance(value["evidence"], list)):
                raise ReviewError("semantic_history_contract_mismatch")
            records = value["evidence"]
            if ((value["status"] == "ok" and not 1 <= len(records) <= 12)
                    or (value["status"] != "ok" and records)
                    or any(not isinstance(r, dict) or set(r) != {"source_id", "source_type", "text", "date"}
                        or not _text(r["source_id"], 512) or r["source_type"] not in ("user", "assistant", "tool", "document")
                        or not _text(r["text"], 4096) or (r["date"] is not None and not _text(r["date"], 128)) for r in records)):
                raise ReviewError("semantic_history_contract_mismatch")
            if value["status"] == "unavailable" and not reused:
                return unavailable_review(turn, identifier, "semantic_history_unavailable")
            all_records = []
            for v in [r for e in reused for r in e["evidence"]] + records:
                if v not in all_records: all_records.append(copy.deepcopy(v))
            # Conflicting records with one source ID are ambiguous, not mergeable.
            by_id = {}
            for v in all_records:
                if v["source_id"] in by_id and by_id[v["source_id"]] != v:
                    raise ReviewError("semantic_history_contract_mismatch")
                by_id[v["source_id"]] = v
            if len(all_records) > 12:
                raise ReviewError("semantic_history_contract_mismatch")
            coverage = []
            for q in queries:
                entry = matched.get(key(q["query"]))
                status = ("candidate" if entry and entry["status"] == "ok" else
                          "no_match" if entry else
                          "candidate" if value["status"] == "ok" else
                          "no_match" if value["status"] == "no_match" else "unavailable")
                coverage.append({"query": q["query"], "status": status})
            partial = any(c["status"] == "unavailable" for c in coverage)
            return {"status": "ok", "turn_id": turn, "draft_id": identifier,
                "review": {"mode": "reviewed", "retrieval_status": "partial" if partial else "ok" if all_records else "no_match",
                           "retrieval_started": bool(pending), "approval_provided": False,
                           "evidence": all_records, "warnings": ["partial_history_unavailable"] if partial else [],
                           "target_coverage": coverage,
                           "reuse_count": len(matched)}}
        except Exception as error:
            reason = str(error) if isinstance(error, ReviewError) and str(error) in ("semantic_history_deadline", "semantic_history_contract_mismatch") else "semantic_history_unavailable"
            return unavailable_review(turn, identifier, reason)
