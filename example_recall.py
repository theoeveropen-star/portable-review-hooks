#!/usr/bin/env python3
"""Local-only demonstration adapter for a single owner's JSONL memory file.

Replace search() with your own scoped retrieval. Do not expose this demo on a
network or share the memory file. The request and the response are read-only.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path

VERSION = 'portable-review-recall/v1'
p = argparse.ArgumentParser()
p.add_argument('--file', type=Path, required=True, help='your UTF-8 JSONL source records')
p.add_argument('--port', type=int, default=18889)
a = p.parse_args()

def search(queries):
    # Simple literal search only, for demonstrating the interface. A real
    # memory adapter should replace this with its own scoped retrieval.
    records = []
    for line in a.file.open(encoding='utf-8'):
        record = json.loads(line)
        if set(record) != {'source_id', 'source_type', 'text', 'date'}:
            raise ValueError('record_format')
        if any(q['query'].casefold() in record['text'].casefold() for q in queries):
            records.append(record)
            if len(records) == 12: break
    return records

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_POST(self):
        if self.path != '/recall': self.send_error(404); return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 32768: raise ValueError('size')
            raw = self.rfile.read(size)
            q = json.loads(raw)
            if set(q) != {'version', 'session_id', 'turn_id', 'draft_id', 'current_user', 'recall_scope', 'queries'} or q['version'] != VERSION:
                raise ValueError('request')
            if not isinstance(q['queries'], list) or not 1 <= len(q['queries']) <= 4:
                raise ValueError('queries')
            evidence = search(q['queries'])
            status = 'ok' if evidence else 'no_match'
        except Exception:
            # Invalid requests or unreadable storage are not successful misses.
            self.send_error(400); return
        response = {'version': VERSION, 'session_id': q['session_id'], 'turn_id': q['turn_id'],
            'draft_id': q['draft_id'], 'request_sha256': hashlib.sha256(raw).hexdigest(),
            'status': status, 'evidence': evidence}
        body = json.dumps(response, ensure_ascii=False, separators=(',', ':')).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

HTTPServer(('127.0.0.1', a.port), Handler).serve_forever()
