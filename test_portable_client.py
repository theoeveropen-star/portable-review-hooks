"""Synthetic provider and recall checks; never connects to a real model or memory."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from chat_tools.reply_review import draft_id, ReviewError
from claude_chat_adapter.draft_review_contract import build_draft_review_input
import claude_chat_adapter.draft_review_client as provider
from portable_client import PortableReviewClient


def main():
    case = {"status": "ok", "bad_binding": False, "bad_plan": False, "oversize": False}

    class RecallHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = json.loads(body)
            evidence = [{"source_id": "synthetic:1", "source_type": "user",
                         "text": "synthetic record", "date": None}] if case["status"] == "ok" else []
            if case["oversize"] and evidence:
                evidence[0]["text"] = "x" * 4097
            result = {**{k: request[k] for k in ("version", "session_id", "turn_id", "draft_id")},
                      "request_sha256": hashlib.sha256(body).hexdigest(),
                      "status": case["status"], "evidence": evidence}
            if case["bad_binding"]:
                result["turn_id"] = "wrong"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        def log_message(self, *args):
            pass

    class Response:
        status = 200

        def __init__(self):
            plan = ({"route": "pass", "issues": []} if not case["bad_plan"] else
                    {"route": "pass", "issues": [{"text": "invented", "kind": "history_dependency",
                                                    "query": "x", "current_evidence_ids": []}]})
            self.data = json.dumps({"choices": [{"finish_reason": "stop",
                                                   "message": {"content": json.dumps(plan)}}]}).encode()

        def read1(self, count):
            part, self.data = self.data[:count], self.data[count:]
            return part

    class SyntheticProviderConnection:
        sock = None

        def set_debuglevel(self, level):
            pass

        def request(self, method, path, body, headers):
            assert method == "POST" and path == "/v1/chat/completions"
            assert json.loads(body)["messages"][0]["role"] == "system"

        def getresponse(self):
            return Response()

        def close(self):
            pass

    server = HTTPServer(("127.0.0.1", 0), RecallHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Replace network transport only; actual provider serialization and parser run.
    provider._public_target = lambda value: urlsplit("https://example.invalid")
    provider.https_connection = lambda *args, **kwargs: SyntheticProviderConnection()
    try:
        client = PortableReviewClient("https://example.invalid", "test-model", "synthetic-key-123",
                                      f"http://127.0.0.1:{server.server_port}/recall")
        session, turn, draft = "synthetic-session", "synthetic-turn", "synthetic draft"
        identifier = draft_id(session, turn, draft)
        review_input = build_draft_review_input(current_user="synthetic user", recent_context=[], draft=draft)
        result = client.classify_draft(session, turn, identifier, draft, review_input)
        assert result["status"] == "ok" and result["work"]["route"] == "pass"
        case["bad_plan"] = True
        result = client.classify_draft(session, turn, identifier, draft, review_input)
        assert result["status"] == "unavailable" and result["plan"] is None
        queries = [{"text": draft, "kind": "uncertain", "query": "synthetic query"}]
        result = client.semantic_review(session, turn, identifier, draft, queries)
        assert result["status"] == "ok" and result["review"]["evidence"][0]["text"] == "synthetic record"
        case["status"] = "no_match"
        result = client.semantic_review(session, turn, identifier, draft, queries)
        assert result["status"] == "ok" and result["review"]["retrieval_status"] == "no_match"
        case["status"] = "unavailable"
        assert client.semantic_review(session, turn, identifier, draft, queries)["status"] == "unavailable"
        case.update(status="ok", bad_binding=True)
        assert client.semantic_review(session, turn, identifier, draft, queries)["status"] == "unavailable"
        case.update(bad_binding=False, oversize=True)
        assert client.semantic_review(session, turn, identifier, draft, queries)["status"] == "unavailable"
        try:
            client.semantic_review(session, turn, "wrong", draft, queries)
        except ReviewError:
            pass
        else:
            raise AssertionError("wrong draft accepted")
        print("8 synthetic checks passed: provider payload/parser + strict plan, history evidence/no-match/unavailable/binding/size, draft binding")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
