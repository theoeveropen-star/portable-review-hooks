"""PreToolUse guard, invoked by Claude before each tool execution.

Stores only hashes and tool names, never arguments or tool results. Unknown
state is denied. This hook is supplied explicitly through --settings.
"""
import argparse
import hashlib
import json
import sqlite3
import sys


# Only explicit read/poll tools receive a separate finite repeat allowance.
POLL_REPEAT_LIMITS = {"mcp__house_music__listen_music": 8}
LIMIT_MESSAGES = {
    "tool_repeat_limit": "此工具相同参数的重复次数已到上限，本轮不要再调用它。请用已有结果回复；尚未完成就如实说明，不能编造结果。",
    "tool_call_limit": "本轮工具调用次数已到上限，不要再调用工具。请用已有信息完成回复，并说明未完成的部分。",
}

def decide(state, session, client, value, max_calls, max_repeat, allowed_tools=None):
    identifier = value.get("tool_use_id")
    name = value.get("tool_name")
    if not isinstance(identifier, str) or not 0 < len(identifier) <= 160 or not isinstance(name, str) or not 0 < len(name) <= 96:
        return "tool_guard_invalid_input"
    if allowed_tools is not None and name not in allowed_tools:
        return "tool_not_allowed"
    fingerprint = hashlib.sha256(json.dumps([name, value.get("tool_input")], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    db = sqlite3.connect(state, timeout=3)
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status,guard_error,receipt_json FROM claude_turns WHERE session_id=? AND client_message_id=?", (session, client)).fetchone()
        if not row or row[0] != "running":
            return "tool_guard_inactive_turn"
        if row[1]:
            return row[1]
        report = json.loads(row[2] or '{}')
        review = report.get('reply_review_hook') if isinstance(report, dict) else None
        # Check the durable host-owned seal before the prior-ID shortcut: a
        # pre-review permission receipt cannot authorize a post-review replay.
        if isinstance(review, dict) and review.get('tools_sealed') is True:
            reason = "reply_review_tools_sealed"
            db.execute("UPDATE claude_turns SET guard_error=? WHERE session_id=? AND client_message_id=?", (reason, session, client))
            db.commit()
            return reason
        prior = db.execute("SELECT fingerprint FROM claude_tools WHERE session_id=? AND client_message_id=? AND tool_id=?", (session, client, identifier)).fetchone()
        if prior:
            return None if prior[0] == fingerprint else "tool_guard_id_conflict"
        calls, repeated = db.execute("SELECT COUNT(*),COALESCE(SUM(fingerprint=?),0) FROM claude_tools WHERE session_id=? AND client_message_id=?", (fingerprint, session, client)).fetchone()
        reason = "tool_call_limit" if calls >= max_calls else "tool_repeat_limit" if repeated >= POLL_REPEAT_LIMITS.get(name, max_repeat) else None
        # A budget denial is a tool result, not a poisoned turn. Security and
        # review-seal failures above remain fatal; wall/API budgets still bound execution.
        if not reason:
            db.execute("INSERT INTO claude_tools VALUES(?,?,?,?,?)", (session, client, identifier, fingerprint, name))
        db.commit()
        return reason
    finally:
        db.close()


def running_client(state, session, native):
    """The one running turn of this session on this native transcript (hook payload session_id).

    The adapter drives one turn per session at a time; anything else is denied.
    Kept out of argv so the --settings text is identical on every turn.
    """
    if not isinstance(native, str) or not 0 < len(native) <= 160:
        return None
    db = sqlite3.connect(state, timeout=3)
    try:
        rows = db.execute("SELECT client_message_id FROM claude_turns WHERE session_id=? AND native_id=? AND status='running'",
                          (session, native)).fetchall()
    finally:
        db.close()
    return rows[0][0] if len(rows) == 1 else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--client")  # older settings only; current ones look the turn up
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--max-repeat", type=int, required=True)
    parser.add_argument("--allowed-tools-json")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("oversized")
        value = json.loads(raw)
        allowed = json.loads(args.allowed_tools_json) if args.allowed_tools_json is not None else None
        if allowed is not None and (not isinstance(allowed, list) or not all(isinstance(name, str) for name in allowed)):
            raise ValueError("invalid_allowlist")
        client = args.client or running_client(args.state, args.session, value.get("session_id"))
        reason = decide(args.state, args.session, client, value, args.max_calls, args.max_repeat, allowed) if client else "tool_guard_inactive_turn"
    except Exception:
        reason = "tool_guard_unavailable"
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": LIMIT_MESSAGES.get(reason, reason)}}))


if __name__ == "__main__":
    main()
