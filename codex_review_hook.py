#!/usr/bin/env python3
"""Private Codex hook transport. The owner makes every review decision."""
import argparse, http.client, json, sys
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument('--route',required=True); p.add_argument('--event',choices=['Stop','PreToolUse','PostToolUse'],required=True)
    a=p.parse_args(); route=json.loads(Path(a.route).read_text())
    body=sys.stdin.buffer.read(1114113)
    if len(body)>1114112: raise ValueError('too_large')
    event=json.loads(body)
    if event.get('hook_event_name') != a.event: raise ValueError('event_mismatch')
    conn=http.client.HTTPConnection('127.0.0.1',route['port'],timeout=45)
    try:
        conn.request('POST','/'+a.event,body,{'Authorization':'Bearer '+route['token'],'Content-Type':'application/json'})
        res=conn.getresponse(); raw=res.read(65537)
        if res.status!=200 or len(raw)>65536: raise ValueError('host_refused')
        print(json.dumps(json.loads(raw),ensure_ascii=False))
    finally: conn.close()
if __name__=='__main__':
    try: main()
    except Exception:
        # Stop errors do not prove a review; the buffered host refuses to publish.
        print('review_host_callback_failed',file=sys.stderr);raise SystemExit(2)
