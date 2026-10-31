"""Readable evidence only after the existing typed-source verifier succeeds."""
import json
import hashlib

def source_binding(value, readable):
    """Aliases only already verified host sources, never a body-declared author."""
    provenance=value.get('provenance') or {};view=value.get('view') or {}
    source_id=value.get('source_label')
    if not source_id and value.get('window') and value.get('inbox_seq') is not None:
        source_id='inbox:'+str(value['window'])+':'+str(value['inbox_seq'])
    if value.get('kind')=='current_execution_receipt' and provenance.get('tool_call_id'):
        source_id='tool-call:'+provenance['tool_call_id']
    if not source_id:return None
    role=value.get('role');event_time=value.get('event_time')
    if value.get('kind')=='current_execution_receipt':
        try:event_time=json.loads(value['content']).get('observed_at')
        except (ValueError,TypeError,KeyError,AttributeError):return None
    state=readable.get('text_state','execution_receipt')
    identity=[value.get('kind'),source_id,role,event_time,state,value.get('source_identity_sha256'),
        view.get('source_sha256'),provenance.get('window'),provenance.get('turn'),
        provenance.get('receipt_sha256'),view.get('byte_range'),view.get('source_bytes'),readable.get('content')]
    version=hashlib.sha256(json.dumps(identity,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return {'source_id':source_id,'source_version':version,'role':role,'event_time':event_time,
        'text_state':state,'proof_scope':('assistant_statement_only' if role=='assistant' else
            'user_statement_at_recorded_time' if role=='user' else 'bound_tool_result_only'),
        'content_sha256':hashlib.sha256(readable.get('content','').encode()).hexdigest()}

def readable_evidence(value):
    if not isinstance(value, dict): return value
    kind = value.get('kind')
    out = {k: value[k] for k in ('version', 'kind', 'role', 'source_label', 'event_time', 'provisional') if k in value}
    if kind in ('delivered_original', 'delivered_archive_original'):
        out['text_state'] = value.get('text_state', (value.get('view') or {}).get('text_state', 'excerpt'))
        out['content'] = value.get('content', '')
        out['authority'] = '历史记录；原角色、原时间和全文或节选范围见本条。节选以外未交付，来源定位信息在内部记录中保留。'
    elif kind == 'current_execution_receipt':
        raw = value.get('content', '')
        try: receipt = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError): receipt = {'text': raw}
        if not isinstance(receipt, dict): receipt = {'text': receipt}
        # Hashes authenticate before this projection. Actual operation/result remain.
        out['content'] = json.dumps({k: receipt[k] for k in ('tool_name','operation','target','status','result','error','bytes_written','source','destination','observed_at') if k in receipt}, ensure_ascii=False, separators=(',', ':'))
        out['provenance'] = {'scope': 'isolated_local_execution_fixture'}
        out['authority'] = '本轮已绑定的隔离执行回执；操作、目标、状态与结果见本条。生产宿主认证未验证。'
    else:
        return value
    binding=source_binding(value,out)
    if binding:out['source_binding']=binding
    return out
