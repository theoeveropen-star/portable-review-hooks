import hashlib,json,os,sys,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
root=Path(__file__).resolve().parent;sys.path[:0]=[str(root),str(root/'lib')]
from prefetch_reuse import acquire
import portable_client as pc
from claude_chat_adapter.draft_review_contract import build_draft_review_input
from claude_chat_adapter.draft_review_host import revision_basis
from chat_tools.reply_review import draft_id
state={'mode':'valid','requests':[]}; rec={'source_id':'s1','source_type':'user','text':'SYNTHETIC_EVIDENCE_ONE','date':'2026-10-01'}
class H(BaseHTTPRequestHandler):
 def log_message(self,*_):pass
 def do_POST(self):
  raw=self.rfile.read(int(self.headers['Content-Length']));q=json.loads(raw);state['requests'].append((self.path,q))
  if self.path=='/prefetch':
   e=[{'scope':'shared','query':'exact query','status':'ok','evidence':[rec]}]
   if state['mode']=='duplicate':e.append(dict(e[0]))
   if state['mode']=='oversize':e[0]['evidence'][0]['text']='x'*6001
   data={**{k:q[k] for k in ('version','session_id','turn_id')},'request_sha256':hashlib.sha256(raw).hexdigest(),'entries':e}
   if state['mode']=='bad_binding':data['turn_id']='wrong'
  else:
   if state['mode']=='remote_unavailable':self.send_response(503);self.end_headers();return
   data={**{k:q[k] for k in ('version','session_id','turn_id','draft_id')},'request_sha256':hashlib.sha256(raw).hexdigest(),'status':'no_match','evidence':[]}
  body=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=server.serve_forever,daemon=True).start();os.environ['PORTABLE_PREFETCH_URL']=f'http://127.0.0.1:{server.server_port}/prefetch'
try:
 item=acquire('s','t','synthetic user');assert item
 for mode in ('duplicate','bad_binding','oversize'):
  state['mode']=mode;assert acquire('s','t','synthetic user') is None
  rec['text']='SYNTHETIC_EVIDENCE_ONE'
 state['mode']='valid'
 client=pc.PortableReviewClient('https://example.invalid','synthetic','synthetic-key',f'http://127.0.0.1:{server.server_port}/recall')
 client.set_delivered_prefetch(item);client.current_user='synthetic user';client.recall_scope='shared';client.classifier_saw_prefetch=True
 draft='BLUE';did=draft_id('s','t',draft)
 def q(query):return {'text':draft,'kind':'uncertain','query':query}
 before=len(state['requests']);r=client.semantic_review('s','t',did,draft,[q('exact query')]);assert len(state['requests'])==before and r['review']['evidence']==[rec] and r['review']['reuse_count']==1
 r=client.semantic_review('s','t',did,draft,[q('exact query'),q('different query')]);assert state['requests'][-1][1]['queries']==[q('different query')]
 assert r['review']['target_coverage']==[{'query':'exact query','status':'candidate'},{'query':'different query','status':'no_match'}]
 work={'route':'history_review','issues':[{'kind':'history_dependency','query':'exact query'},{'kind':'history_dependency','query':'different query'}],'current_evidence':[]}
 basis=revision_basis(work,r);assert len(basis['issues'])==1 and basis['support_gap'] and basis['unresolved_history_issue_count']==1
 state['mode']='remote_unavailable';r=client.semantic_review('s','t',did,draft,[q('exact query'),q('other')]);assert r['status']=='ok' and r['review']['retrieval_status']=='partial' and r['review']['target_coverage'][1]['status']=='unavailable' and r['review']['evidence']==[rec]
 state['mode']='valid'
 client.recall_scope='work';before=len(state['requests']);client.semantic_review('s','t',did,draft,[q('exact query')]);assert len(state['requests'])==before+1
 client.recall_scope='shared';client.classifier_saw_prefetch=True;before=len(state['requests']);client.semantic_review('s','t',did,draft,[q('exact query'),q('exact  query')]);assert len(state['requests'])==before+1 and len(state['requests'][-1][1]['queries'])==2
 client.recall_scope='shared';client.classifier_saw_prefetch=False;before=len(state['requests']);client.semantic_review('s','t',did,draft,[q('exact query')]);assert len(state['requests'])==before+1
 from prefetch_reuse import in_classifier
 assert in_classifier(item['packet'],{'recent_context':[{'role':'tool','content':item['text']}]})
 assert not in_classifier(item['packet'],{'recent_context':[{'role':'tool','content':'omitted'}]})
 print('prefetch synthetic: binding, collision, size, exact-scope, omitted, partial-query and coverage passed')
finally:server.shutdown();server.server_close()
