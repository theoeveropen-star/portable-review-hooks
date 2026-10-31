#!/usr/bin/env python3
"""Text-only stdio MCP bridge; all authority and reading stay in the host."""
import argparse,http.client,json,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--route',required=True);args=p.parse_args()
route=json.loads(Path(args.route).read_text())
def rpc(method,params):
 body=json.dumps({'method':method,'params':params},ensure_ascii=False).encode()
 c=http.client.HTTPConnection('127.0.0.1',route['port'],timeout=8)
 try:
  c.request('POST','/ReadTool',body,{'Authorization':'Bearer '+route['token'],'Content-Type':'application/json'})
  r=c.getresponse();raw=r.read(65537)
  if r.status!=200 or len(raw)>65536:raise ValueError()
  return json.loads(raw)
 finally:c.close()
tools=[{'name':'read','description':'Read a UTF-8 regular file inside the configured read root. Relative paths only, symlinks are refused.','inputSchema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}},
{'name':'list','description':'List one directory inside the configured read root. Relative path, use . for root.','inputSchema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}},
{'name':'search','description':'Find a literal string in one UTF-8 file inside the configured read root.','inputSchema':{'type':'object','properties':{'path':{'type':'string'},'query':{'type':'string'}},'required':['path','query'],'additionalProperties':False}}]
for item in tools:item['annotations']={'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False}
for line in sys.stdin:
 try:
  if len(line.encode())>65536:continue
  q=json.loads(line);method=q.get('method');ident=q.get('id');params=q.get('params',{})
  if method=='initialize':result={'protocolVersion':params.get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},'serverInfo':{'name':'portable-read','version':'1'}}
  elif method=='tools/list':result={'tools':tools}
  elif method=='tools/call':
   try:result=rpc(params.get('name'),params.get('arguments'))
   except Exception:result={'content':[{'type':'text','text':'Read unavailable.'}],'isError':True}
  elif method=='ping':result={}
  else:
   if ident is None:continue
   print(json.dumps({'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'Method not found'}}),flush=True);continue
  if ident is not None:print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result},ensure_ascii=False),flush=True)
 except Exception:
  if isinstance(locals().get('q'),dict) and q.get('id') is not None:print(json.dumps({'jsonrpc':'2.0','id':q['id'],'error':{'code':-32603,'message':'Invalid request'}}),flush=True)
