"""Bounded exact excerpts of completed tool replies, including archive locators."""
import hashlib,json,re

def project_tool_record(record, budget=7600):
    if len(record.encode())<=budget:return record
    obj=json.loads(record)
    response=obj.get('delivered_response')
    refs=[];texts=[]
    def walk(value,key='',depth=0):
        if depth>14:return
        if isinstance(value,dict):
            for k,v in value.items():walk(v,k,depth+1)
        elif isinstance(value,list):
            for v in value:walk(v,key,depth+1)
        elif isinstance(value,str):
            if key in ('archive_ref','item_id','fragment_id','handle','ref_id','source_ref'):
                if len(value)<512:refs.append({key:value})
            if key in ('text','body','excerpt','summary','cue','content'):
                if value.startswith(('{','[')):
                    try:walk(json.loads(value),key,depth+1);return
                    except (ValueError,TypeError):pass
                if value:texts.append(value)
    walk(response)
    excerpts=[]
    base={'tool':obj.get('tool'),'arguments':obj.get('arguments'),'http_status':obj.get('http_status'),
          'view':'excerpt','response_sha256':hashlib.sha256(record.encode()).hexdigest(),
          'source_refs':refs[:24], 'excerpts':excerpts,
          'coverage':'Only the quoted excerpts were supplied to this reviewer; omitted material is unread.'}
    query=str(obj.get("arguments",{}).get("query", ""))
    terms=re.findall(r"[A-Za-z][A-Za-z0-9_]+|[\u3400-\u9fff]{2}",query)
    texts=sorted(texts,key=lambda t:sum(term.lower() in t.lower() for term in terms),reverse=True)
    for text in texts:
        # Retain complete sentences; long sentences are explicitly excerpted.
        for sentence in re.split(r'(?<=[。！？!?])|\n',text):
            sentence=sentence.strip()
            if not sentence:continue
            trial=sentence[:900]
            excerpts.append({'text':trial,'partial':len(trial)<len(sentence)})
            if len(json.dumps(base,ensure_ascii=False,separators=(',',':')).encode())>budget:
                excerpts.pop();break
    while len(json.dumps(base,ensure_ascii=False,separators=(',',':')).encode())>budget and base['source_refs']:base['source_refs'].pop()
    return json.dumps(base,ensure_ascii=False,separators=(',',':'))
