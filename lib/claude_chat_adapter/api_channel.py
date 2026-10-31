"""Owner-only BYOK settings and credential/budget boundary for the native CLI.

The native process receives only a one-turn loopback credential. Real provider
keys never enter CLI environment, arguments, transcript, tool subprocesses or DB.
Only an explicit provider-model lookup contacts the provider outside a turn.
"""
import contextlib
import datetime
import hmac
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlencode
from zoneinfo import ZoneInfo
from outbound_proxy import https_connection

OFFICIAL = 'https://api.anthropic.com'
# Official documented context windows, checked 2026-09-25. Custom endpoints must
# declare their actual capacity; an identical name is not capacity evidence.
CATALOG = {'claude-opus-5-5': 1000000, 'claude-sonnet-5': 1000000,
           'claude-haiku-4-5-20251001': 200000}
# Provider IDs are opaque JSON/argv values, often Unicode aliases. Do not use
# an English-name allowlist. Bound length and reject controls, bidi overrides,
# surrounding whitespace and leading '-' (CLI option ambiguity).
MODEL = re.compile(r'(?![-\s])(?!.*\s\Z)[^\x00-\x1f\x7f-\x9f\ud800-\udfff\u2028-\u202e\u2066-\u2069]{1,512}\Z')
FIELDS = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
PRICE_FIELDS = ('input', 'cache_read', 'cache_write', 'output')

class APIError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.status=status

UPSTREAM_TIMEOUT = 120
MODEL_LIST_TIMEOUT = 25
MODEL_LIST_PAGES = 20
MODEL_LIST_COUNT = 2000
MODEL_LIST_BYTES = 2 * 1024 * 1024

def public_target(base_url):
    u=urlsplit(endpoint(base_url) or OFFICIAL)
    addresses=socket.getaddrinfo(u.hostname,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise APIError('api_invalid_base_url')
    return u


def day(): return datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
def number(v): return type(v) in (int,float) and math.isfinite(v) and 0 <= v <= 1000000

def endpoint(value):
    if not isinstance(value,str) or len(value)>2048: raise APIError('api_invalid_base_url')
    value=value.strip().rstrip('/')
    if not value: return ''
    u=urlsplit(value)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.port not in (None,443):
        raise APIError('api_invalid_base_url')
    # /v1 is commonly copied from provider documentation. Normalize once.
    return value[:-3] if value.endswith('/v1') else value

class Channel:
    def __init__(self, adapter):
        self.a=adapter
        self.folder=Path(adapter.state_path+'.api-private')
        self.folder.mkdir(mode=0o700,exist_ok=True);self.folder.chmod(0o700)
        self.path=self.folder/'settings.json'
        self.settings=json.loads(self.path.read_text()) if self.path.exists() else {'base_url':'','model':'claude-sonnet-5','models':{},'daily_limit':None}
        if self.path.exists():self.path.chmod(0o600)
        self.a.db.executescript('''CREATE TABLE IF NOT EXISTS api_requests(
            id TEXT PRIMARY KEY,session_id TEXT NOT NULL,client_id TEXT NOT NULL,day TEXT NOT NULL,
            reserved REAL NOT NULL,cost REAL,usage TEXT,status TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS api_turns(session_id TEXT NOT NULL,client_id TEXT NOT NULL,
            model TEXT NOT NULL,config TEXT NOT NULL,PRIMARY KEY(session_id,client_id));''')
        self.a.db.execute("UPDATE api_requests SET status='unknown' WHERE status='pending'");self.a.db.commit()

    def public(self):
        with self.a.lock:
            s=self.settings
            return {'base_url':s['base_url'],'key_last4':s.get('key','')[-4:], 'has_key':bool(s.get('key')),
                    'model':s['model'],'models':s['models'],'daily_limit':s['daily_limit'],
                    'catalog':[{'id':m,'context_window':w} for m,w in CATALOG.items()], 'today':self.today()}

    def list_models(self):
        # Snapshot under the lock; lookup never mutates settings or the ledger.
        with self.a.lock:
            key=self.settings.get('key')
            base=self.settings['base_url']
        if not key:raise APIError('api_key_required')
        models={};cursor=None;seen=set();deadline=time.monotonic()+MODEL_LIST_TIMEOUT
        try:
            for page in range(MODEL_LIST_PAGES):
                connection=None
                try:
                    u=public_target(base)
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError()
                    headers={'Accept':'application/json'}
                    # Custom gateways may restrict their catalog when Anthropic
                    # headers are present. Discover their full OpenAI catalog.
                    if u.hostname.lower()=='api.anthropic.com':
                        headers.update({'x-api-key':key,'anthropic-version':'2023-06-01'})
                    else:headers['Authorization']='Bearer '+key
                    connection=https_connection(u.hostname,443,timeout=min(UPSTREAM_TIMEOUT,remaining))
                    # Disable stdlib debug output even if a caller enabled it globally.
                    connection.set_debuglevel(0)
                    path=u.path.rstrip('/')+'/v1/models'
                    if cursor:path+='?'+urlencode({'after_id':cursor})
                    connection.request('GET',path,headers=headers)
                    response=connection.getresponse()
                    if response.status!=200:
                        raise APIError('api_provider_rejected',response.status if 400<=response.status<600 else 502)
                    chunks=[];size=0
                    while True:
                        remaining=deadline-time.monotonic()
                        if remaining<=0:raise TimeoutError()
                        if connection.sock is not None:connection.sock.settimeout(remaining)
                        chunk=response.read1(min(65536,MODEL_LIST_BYTES+1-size))
                        if not chunk:break
                        chunks.append(chunk);size+=len(chunk)
                        if size>MODEL_LIST_BYTES:raise APIError('api_response_too_large',502)
                    raw=b''.join(chunks)
                    if time.monotonic()>deadline:raise TimeoutError()
                    value=json.loads(raw)
                    if not isinstance(value,dict) or not isinstance(value.get('data'),list):
                        raise APIError('api_invalid_models_response',502)
                    for item in value['data']:
                        if not isinstance(item,dict):continue
                        mid=item.get('id')
                        if not isinstance(mid,str) or not MODEL.fullmatch(mid) or key in mid:continue
                        row=models.setdefault(mid,{'id':mid})
                        name=item.get('display_name')
                        if isinstance(name,str):row['display_name']=name.replace(key,'[redacted]')[:512]
                        for field in ('context_window','context_length','max_context_length','max_input_tokens'):
                            window=item.get(field)
                            if type(window) is int and 0<window<=2000000:
                                row['context_window']=window;break
                        if len(models)>MODEL_LIST_COUNT:raise APIError('api_models_limit_exceeded',502)
                    if value.get('has_more') is not True:
                        return {'models':[models[m] for m in sorted(models)]}
                    cursor=value.get('after_id') or value.get('last_id')
                    if not cursor and value['data'] and isinstance(value['data'][-1],dict):cursor=value['data'][-1].get('id')
                    if not isinstance(cursor,str) or not cursor or len(cursor)>512 or key in cursor or cursor in seen:
                        raise APIError('api_invalid_models_response',502)
                    seen.add(cursor)
                finally:
                    if connection:connection.close()
            raise APIError('api_models_limit_exceeded',502)
        except APIError:raise
        except (ValueError,UnicodeError):raise APIError('api_invalid_models_response',502) from None
        except Exception:raise APIError('api_connection_failed',502) from None

    def today(self):
        rows=self.a.db.execute('SELECT cost,reserved,status FROM api_requests WHERE day=?',(day(),)).fetchall()
        unknown=self.a.db.execute("SELECT COUNT(*) FROM api_requests WHERE status='unknown'").fetchone()[0]
        return {'date':day(),'timezone':'Asia/Shanghai','currency':'USD','estimated_cost':sum(r[0] or 0 for r in rows),
                'reserved':sum(r[1] for r in rows if r[2] in ('pending','unknown')),'unpriced_requests':sum(r[0] is None and r[2]=='complete' for r in rows),
                'uncertain_requests':unknown}

    def save(self,v):
        if not isinstance(v,dict) or set(v)-{'base_url','api_key','clear_key','model','context_window','prices','daily_limit'}:raise APIError('api_invalid_settings')
        with self.a.lock:
            if self.a.active or self.a.db.execute("SELECT 1 FROM claude_slim_jobs WHERE phase NOT IN ('completed','failed') LIMIT 1").fetchone():raise APIError('api_settings_busy')
            s=json.loads(json.dumps(self.settings))
            s['base_url']=endpoint(v.get('base_url',s['base_url']))
            model=v.get('model',s['model'])
            if not isinstance(model,str) or not MODEL.fullmatch(model):raise APIError('api_invalid_model')
            if v.get('clear_key') is True:s.pop('key',None)
            key=v.get('api_key')
            if key:
                if not isinstance(key,str) or not 8<=len(key)<=4096 or any(ord(c)<33 or ord(c)>126 for c in key):raise APIError('api_invalid_key')
                s['key']=key
            window=v.get('context_window')
            if not s['base_url'] and model in CATALOG:window=CATALOG[model]
            if type(window) is not int or not 32768<=window<=2000000:raise APIError('api_context_window_required')
            prices=v.get('prices')
            if prices is not None and (not isinstance(prices,dict) or set(prices)!=set(PRICE_FIELDS) or any(not number(n) for n in prices.values())):raise APIError('api_invalid_prices')
            limit=v.get('daily_limit',s['daily_limit'])
            if limit is not None and not number(limit):raise APIError('api_invalid_limit')
            if limit is not None and prices is None:raise APIError('api_prices_required_for_limit')
            s.update(model=model,daily_limit=limit)
            if s['base_url']!=self.settings['base_url']:s['models']={}
            s['models'][model]={'context_window':window,'prices':prices}
            # Do not permit the credential in any field returned to a client/model.
            if s.get('key') and s['key'] in json.dumps({k:x for k,x in s.items() if k!='key'}):raise APIError('api_secret_in_settings')
            fd,tmp=tempfile.mkstemp(dir=self.folder)
            try:
                with os.fdopen(fd,'w') as f:json.dump(s,f);f.flush();os.fsync(f.fileno())
                os.replace(tmp,self.path)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
            self.settings=s
            return self.public()

    def config(self,model):
        s=self.settings
        if not s.get('key'):raise APIError('api_key_required')
        item=s['models'].get(model)
        if item is None:
            if s['base_url'] or model not in CATALOG:raise APIError('api_context_window_required')
            item={'context_window':CATALOG[model],'prices':None}
        return {'model':model,'base_url':s['base_url'],**item}

    def check(self,config,reserve=0):
        t=self.today();limit=self.settings['daily_limit']
        # Keep unknown charges visible; only a configured spending cap needs
        # to hold future requests until those charges can be reconciled.
        if limit is not None and t['uncertain_requests']:raise APIError('api_usage_uncertain')
        if limit is not None:
            if config['prices'] is None or t['unpriced_requests']:raise APIError('api_prices_required_for_limit')
            if t['estimated_cost']+t['reserved']>=limit-1e-10 or t['estimated_cost']+t['reserved']+reserve>limit+1e-10:raise APIError('api_daily_limit_reached')

    def admit(self,session,client,model):
        config=self.config(model);self.check(config, (config['context_window']*max(config['prices'][k] for k in PRICE_FIELDS[:3])+self.a.slim.policy.output_reserve*config['prices']['output'])/1e6 if config['prices'] else 0)
        self.a.db.execute('INSERT INTO api_turns VALUES(?,?,?,?)',(session,client,model,json.dumps(config)))

    def turn_config(self,session,client):
        row=self.a.db.execute('SELECT config FROM api_turns WHERE session_id=? AND client_id=?',(session,client)).fetchone()
        return json.loads(row[0]) if row else None

    def latest_config(self,session):
        row=self.a.db.execute("SELECT session_id,client_message_id FROM claude_turns WHERE session_id=? AND status IN ('running','completed') ORDER BY (status='running') DESC,created_at DESC,rowid DESC LIMIT 1",(session,)).fetchone()
        return self.turn_config(*row) if row else None

    def reserve(self,session,client,config,body):
        prices=config['prices']
        # Reserve a worst-case complete context at the most expensive input rate,
        # including cold writes, plus maximum output, before EACH native request.
        output=body.get('max_tokens')
        if type(output) is not int or output<1 or output>128000:raise APIError('api_invalid_output_limit')
        reserve=(config['context_window']*max(prices[k] for k in PRICE_FIELDS[:3])+output*prices['output'])/1e6 if prices else 0
        with self.a.lock:
            self.check(config,reserve)
            rid=secrets.token_hex(16)
            self.a.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,NULL,NULL,?,?)',(rid,session,client,day(),reserve,'pending',time.time()));self.a.db.commit()
        return rid

    def settle(self,rid,config,usage,complete):
        valid=complete and isinstance(usage,dict) and all(type(usage.get(k)) is int and usage[k]>=0 for k in FIELDS)
        prices=config['prices'];cost=sum(usage[k]*prices[p] for k,p in zip(FIELDS,PRICE_FIELDS))/1e6 if valid and prices else None
        with self.a.lock:
            self.a.db.execute('UPDATE api_requests SET cost=?,usage=?,status=? WHERE id=?',(cost,json.dumps(usage) if valid else None,'complete' if valid else 'unknown',rid));self.a.db.commit()

    def receipt(self,session,client):
        with self.a.lock:
            if not self.turn_config(session,client):return None
            rows=self.a.db.execute('SELECT cost,usage,status FROM api_requests WHERE session_id=? AND client_id=?',(session,client)).fetchall()
        usage=dict.fromkeys(FIELDS,0)
        for row in rows:
            for k,v in json.loads(row[1] or '{}').items():usage[k]+=v
        return {'channel':'api',**usage,'estimated_cost_usd':sum(r[0] for r in rows) if rows and all(r[0] is not None for r in rows) else None,
                'usage_complete':bool(rows) and all(r[2]=='complete' for r in rows)}

    @contextlib.contextmanager
    def native_environment(self,row):
        config=self.turn_config(row['session_id'],row['client_message_id'])
        if not config:
            yield {};return
        with Relay(self,row,config) as relay:yield relay.environment()

def model_matches_response(requested, observed, evidence):
    # A custom gateway's catalog alias may differ from its response model ID.
    # Accept only names observed on this turn's checked outbound request.
    if requested == observed:return True
    proof=evidence.get(observed) if isinstance(observed,str) else None
    return bool(proof and proof['requested_model']==requested and proof['base_url'])

class Relay:
    def __init__(self,channel,row,config):
        self.channel,self.row,self.config=channel,row,config
        self.token=secrets.token_urlsafe(32)
        self.key=channel.settings['key']
        self.server=None
    def __enter__(self):
        relay=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):relay.handle(self)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();return self
    def __exit__(self,*args):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
    def environment(self):
        m=self.config['model']
        return {'ANTHROPIC_API_KEY':self.token,'ANTHROPIC_AUTH_TOKEN':'','CLAUDE_CODE_OAUTH_TOKEN':'',
                'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{self.server.server_port}',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','ANTHROPIC_CUSTOM_MODEL_OPTION':m,
                'ANTHROPIC_DEFAULT_HAIKU_MODEL':m,'ANTHROPIC_DEFAULT_SONNET_MODEL':m,'ANTHROPIC_DEFAULT_OPUS_MODEL':m,
                'CLAUDE_CODE_SUBAGENT_MODEL':m,'CLAUDE_CODE_AUTO_COMPACT_WINDOW':str(self.config['context_window']),
                # For recognized Claude IDs the CLI only honors MAX_CONTEXT with
                # DISABLE_COMPACT. Host slim remains the sole capacity owner.
                'CLAUDE_CODE_MAX_CONTEXT_TOKENS':str(self.config['context_window']),'DISABLE_COMPACT':'1'}
    def fail(self,h,code,status=400):
        raw=json.dumps({'type':'error','error':{'type':'invalid_request_error','message':code}}).encode()
        h.send_response(status);h.send_header('Content-Type','application/json');h.send_header('Content-Length',str(len(raw)));h.end_headers();h.wfile.write(raw)
    def observe_model(self,message):
        model=message.get('model') if isinstance(message,dict) else None
        if self.config['base_url'] and isinstance(model,str) and MODEL.fullmatch(model) and model!='<synthetic>' and self.key not in model:
            self.row.setdefault('_api_response_models',{})[model]={'requested_model':self.config['model'],'base_url':self.config['base_url'],'source':'provider_response'}

    def handle(self,h):
        rid=None;usage={};complete=False;connection=None;downstream_lost=False
        try:
            auth=h.headers.get('x-api-key','')
            if not hmac.compare_digest(auth,self.token) or h.headers.get('Origin'):return self.fail(h,'unauthorized',401)
            path=urlsplit(h.path).path
            if path not in ('/v1/messages','/v1/messages/count_tokens'):return self.fail(h,'api_endpoint_not_allowed',404)
            size=int(h.headers.get('Content-Length','0'))
            if not 0<size<=32*1024*1024:raise APIError('api_invalid_request')
            body=json.loads(h.rfile.read(size))
            controller=self.row.get('_reply_review')
            if controller is not None and hasattr(controller, 'project_review_feedback'):
                body=controller.project_review_feedback(body)
            if self.row.get('_reference_projection') is not None:
                body=self.row['_reference_projection'].apply(body)
            if body.get('model')!=self.config['model']:raise APIError('api_model_changed')
            if path=='/v1/messages':
                controller=self.row.get('_reply_review')
                if controller is not None and getattr(controller,'semantic',None) is not None:
                    # The authenticated turn host durably owns the single
                    # revision request. Reserve that permission before the
                    # provider ledger or any upstream request can start.
                    try:
                        admitted=controller.admit_native_request()
                        if admitted is not None and admitted is not True:
                            raise ValueError('denied')
                    except Exception:
                        raise APIError('semantic_revision_request_rejected') from None
            u=public_target(self.config['base_url'])
            if path=='/v1/messages':rid=self.channel.reserve(self.row['session_id'],self.row['client_message_id'],self.config,body)
            headers={'content-type':'application/json','x-api-key':self.key,'anthropic-version':h.headers.get('anthropic-version','2023-06-01')}
            if self.config['base_url']:headers['Authorization']='Bearer '+self.key
            if h.headers.get('anthropic-beta'):headers['anthropic-beta']=h.headers['anthropic-beta']
            connection=https_connection(u.hostname,443,timeout=UPSTREAM_TIMEOUT)
            connection.request('POST',u.path.rstrip('/')+path,body=json.dumps(body).encode(),headers=headers)
            response=connection.getresponse()
            if response.status!=200:
                # Provider error bodies can echo headers. Never pass/log them.
                return self.fail(h,'api_provider_rejected',response.status if 400<=response.status<600 else 502)
            content_type=response.getheader('Content-Type','application/json')
            h.send_response(200);h.send_header('Content-Type',content_type);h.send_header('Connection','close');h.end_headers()
            if 'text/event-stream' in content_type:
                while True:
                    line=response.readline(8*1024*1024+1)
                    if not line:break
                    if len(line)>8*1024*1024:raise APIError('api_response_too_large')
                    # Redact before bytes can reach native transcript or tool output.
                    line=line.replace(self.key.encode(),b'[redacted]').replace(json.dumps(self.key)[1:-1].encode(),b'[redacted]')
                    if line.startswith(b'data: '):
                        event=json.loads(line[6:])
                        if event.get('type')=='message_start':
                            self.observe_model(event.get('message',{}))
                            usage.update(event.get('message',{}).get('usage',{}))
                        if event.get('type')=='message_delta':usage.update(event.get('usage',{}))
                        if event.get('type')=='message_stop':complete=True
                    if not downstream_lost:
                        try:h.wfile.write(line);h.wfile.flush()
                        except OSError:downstream_lost=True
                    # Finish reading this already-started response so actual
                    # usage can settle even when the native reader disconnects.
            else:
                raw=response.read(32*1024*1024+1)
                if len(raw)>32*1024*1024:raise APIError('api_response_too_large')
                raw=raw.replace(self.key.encode(),b'[redacted]').replace(json.dumps(self.key)[1:-1].encode(),b'[redacted]');value=json.loads(raw)
                usage=value.get('usage',{});complete=value.get('type')=='message'
                if complete:self.observe_model(value)
                h.wfile.write(raw)
        except Exception as exc:
            if isinstance(exc,APIError):self.row['_api_error']=str(exc)
            try:self.fail(h,str(exc) if isinstance(exc,APIError) else 'api_connection_failed',502)
            except OSError:pass
        finally:
            if connection:connection.close()
            if rid:
                clean={k:usage.get(k,0) if k.startswith('cache_') else usage.get(k) for k in FIELDS}
                self.channel.settle(rid,self.config,clean,complete)
