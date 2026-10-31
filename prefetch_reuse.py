"""Optional trusted per-turn recall delivery; no Brain dependency or writes."""
import hashlib,json,os,unicodedata
from portable_client import _json,_post,_text
VERSION='portable-review-prefetch/v1'
def key(query):
 return ' '.join(unicodedata.normalize('NFC',query).split())
def acquire(session,turn,current_user,timeout=8.0):
 url=os.environ.get('PORTABLE_PREFETCH_URL')
 if not url:return None
 request={'version':VERSION,'session_id':session,'turn_id':turn,'current_user':current_user}
 raw=_json(request)
 try:
  r=_post(url,os.environ.get('PORTABLE_PREFETCH_TOKEN'),raw,max(0.001,min(8.0,timeout)))
  if not isinstance(r,dict) or set(r)!={'version','session_id','turn_id','request_sha256','entries'} or any(r[k]!=request[k] for k in ('version','session_id','turn_id')) or r['request_sha256']!=hashlib.sha256(raw).hexdigest():return None
  entries=r['entries']
  if not isinstance(entries,list) or not 1<=len(entries)<=4:return None
  seen=set();sources={}
  for entry in entries:
   if not isinstance(entry,dict) or set(entry)!={'scope','query','status','evidence'} or entry['scope'] not in ('work','life','shared') or not _text(entry['query'],768) or not key(entry['query']) or entry['status'] not in ('ok','no_match') or not isinstance(entry['evidence'],list):return None
   identity=(entry['scope'],key(entry['query']))
   if identity in seen:return None
   seen.add(identity)
   records=entry['evidence']
   if (entry['status']=='ok' and not 1<=len(records)<=8) or (entry['status']=='no_match' and records):return None
   for v in records:
    if not isinstance(v,dict) or set(v)!={'source_id','source_type','text','date'} or not _text(v['source_id'],512) or v['source_type'] not in ('user','assistant','tool','document') or not _text(v['text'],4096) or (v['date'] is not None and not _text(v['date'],128)):return None
    previous=sources.get(v['source_id'])
    if previous is not None and previous!=v:return None
    sources[v['source_id']]=v
  packet={'type':'portable-review-prefetch-delivered/v1','entries':entries}
  text=_json(packet).decode()
  if len(text.encode())>6000:return None
  return {'packet':packet,'text':text}
 except Exception:return None

def in_classifier(packet,value):
 """Find exact packet as a complete tool source, including nested compact packs."""
 def probe(obj,depth=0):
  if depth>8:return False
  if obj==packet:return True
  if isinstance(obj,list):return any(probe(v,depth+1) for v in obj)
  if isinstance(obj,dict):return any(probe(v,depth+1) for v in obj.values())
  if isinstance(obj,str) and obj.startswith(('{','[')):
   try:return probe(json.loads(obj),depth+1)
   except (TypeError,ValueError):pass
  return False
 return probe(value.get('recent_context',[]))
