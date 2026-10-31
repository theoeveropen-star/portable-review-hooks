#!/usr/bin/env python3
"""Authenticated hook callback; never reads a transcript or reasoning stream."""
import argparse
import http.client
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--route', required=True)
    parser.add_argument('--event', choices=['Stop', 'PostToolUse'], required=True)
    args = parser.parse_args()
    route = json.loads(Path(args.route).read_text())
    payload = json.loads(sys.stdin.buffer.read(1114113))
    if payload.get('session_id') != route['native_id'] or payload.get('hook_event_name') != args.event:
        raise ValueError('hook_binding_invalid')
    if args.event == 'Stop':
        body = {k: payload.get(k) for k in ('session_id', 'last_assistant_message', 'stop_hook_active')}
    else:
        body = {k: payload.get(k) for k in ('session_id', 'tool_use_id', 'tool_name', 'tool_input', 'tool_response')}
    conn = http.client.HTTPConnection('127.0.0.1', route['port'], timeout=36)
    try:
        conn.request('POST', '/' + args.event, json.dumps(body, ensure_ascii=False).encode('utf-8'),
                     {'Authorization': 'Bearer ' + route['token'], 'Content-Type': 'application/json'})
        response = conn.getresponse(); raw = response.read(32769)
        if response.status != 200 or len(raw) > 32768:
            raise ValueError('hook_host_refused')
        value = json.loads(raw)
        print(json.dumps(value, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == '__main__':
    try: main()
    except Exception:
        # The host final guard independently refuses missing/failed Stop proofs.
        # Exit 2 blocks a Stop continuation; no exception payload reaches a model.
        print('review_host_callback_failed', file=sys.stderr)
        raise SystemExit(2)
