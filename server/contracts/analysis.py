"""Content-analysis projection. No native records, thoughts, system events or attachments.

This selects data by meaning/type, NOT by secret names. User prose and tool
parameters (including credentials/signatures) are not redacted. Only recognized
attachments are omitted. All source replay happens locally in transient memory.
"""
from copy import deepcopy
import hashlib
import json
import re
from .models import timestamp_ms

POLICY='analysis-v1'
ATTACHMENTS={'image','input_image','output_image','image_url','input_audio','audio','video','buffer',
             'base64'}
FILE_ATTACHMENTS={'document','input_file','file','attachment','inline_reference','inlinereference'}
DATA_URI=re.compile(r'data:[a-zA-Z0-9.+/-]+(?:;[a-zA-Z0-9=.+_-]+)*;base64,[a-zA-Z0-9+/=_-]+')
OLD_BINARY_MARKER='[binary data retained in original record]'
OMIT=object()
META_KEYS={'formatVersion','historyMode','cliVersion','copilotVersion','repository','modelProvider',
           'sandboxPolicy','approvalMode','parentSessionId','relationship','parentSessionPath','defaultLeaf',
           'rawSessionId','claudeTitle','createdAt','creationDate','projectFromCwd','inheritedContextBoundary','recoveredPreBoundaryRecords',
           'latestUsageRecord','models','providers','legacyArchive','contentPolicy'}
MESSAGE_KEYS={'id','sessionId','source','type','role','uuid','parentUuid','timestamp','timestampUnit',
              'timestampSource','message','rawRecord','toolCallId','status'}


def no_attachments(value):
    if isinstance(value,str):return DATA_URI.sub('',value).replace(OLD_BINARY_MARKER,'')
    if isinstance(value,list):return [x for child in value if (x:=no_attachments(child)) is not OMIT]
    if not isinstance(value,dict):return value
    kind=str(value.get('type',value.get('kind',''))).lower()
    mime=str(value.get('mimeType',value.get('mime_type',value.get('media_type','')))).lower()
    if kind in ATTACHMENTS or value.get('encoding')=='base64' or mime.startswith(('image/','audio/','video/','application/pdf')):
        return OMIT
    # Do not propagate legacy "retained in raw record" placeholders: originals
    # are deliberately not stored by the analysis service.
    if value.get('_omitted') in ('binary','opaque'):return OMIT
    result={}
    for key,child in value.items():
        cleaned=no_attachments(child)
        if cleaned is not OMIT:result[key]=cleaned
    return result


def metadata(value):
    result={k:no_attachments(v) for k,v in value.items() if k in META_KEYS}
    result={k:v for k,v in result.items() if v is not OMIT}
    structured={'legacyArchive','claudeTitle','repository','latestUsageRecord','models','providers','sandboxPolicy'}
    result={k:v for k,v in result.items() if k in structured or v is None or isinstance(v,(str,int,float,bool))}
    if isinstance(result.get('claudeTitle'),dict):
        result['claudeTitle']={k:v for k,v in result['claudeTitle'].items() if k in ('known','custom','slug') and (v is None or isinstance(v,(str,bool)))}
    for key in ('repository','sandboxPolicy'):
        if isinstance(result.get(key),dict):
            allowed={'url','name','branch','root','owner','commit','remote','repo','path','type','writable_roots','network_access','exclude_tmpdir_env_var','exclude_slash_tmp'}
            result[key]={k:v for k,v in result[key].items() if k in allowed and
                         (v is None or isinstance(v,(str,int,float,bool)) or isinstance(v,list) and all(isinstance(x,str) for x in v))}
    for key in ('models','providers'):
        if key in result:result[key]=[x for x in result[key] if isinstance(x,str)] if isinstance(result[key],list) else []
    if isinstance(result.get('legacyArchive'),dict):
        result['legacyArchive']={k:v for k,v in result['legacyArchive'].items() if k in
                                ('version','database','sessionId','originalProject','recordCount','lastUpdateTimeSeconds','sourceAvailable')}
    if isinstance(result.get('latestUsageRecord'),dict):
        record=result['latestUsageRecord'];safe={}
        def counters(value):
            if not isinstance(value,dict):return {}
            return {k:(counters(v) if isinstance(v,dict) else v) for k,v in value.items()
                    if isinstance(v,dict) or isinstance(v,(int,float)) and not isinstance(v,bool)}
        for key in ('usage','turn_token_usage','thread_token_usage'):
            if key in record:safe[key]=counters(record[key])
        for key in ('turn_id','response_id'):
            if isinstance(record.get(key),str):safe[key]=record[key]
        result['latestUsageRecord']=safe
    result['contentPolicy']=POLICY
    return result


def flags(value):
    if not isinstance(value,dict):return None
    for key in ('exitCode','exit_code'):
        code=value.get(key)
        if isinstance(code,int) and not isinstance(code,bool):return code==0
    for key in ('is_error','isError'):
        if isinstance(value.get(key),bool):return not value[key]
    if isinstance(value.get('success'),bool):return value['success']
    status=value.get('status')
    if isinstance(status,str):
        if status.lower() in ('failed','failure','error','cancelled','canceled','interrupted','aborted'):return False
        if status.lower() in ('success','succeeded'):return True
    return None


def status_name(success):return 'success' if success is True else 'failure' if success is False else 'unknown'


def parse_arguments(value):
    if isinstance(value,str):
        try:return json.loads(value)
        except ValueError:pass
    return value


def calls_from(value,found=None):
    """Recover complete original arguments before the old UI compacted them."""
    found={} if found is None else found
    if isinstance(value,list):
        for v in value:calls_from(v,found)
    elif isinstance(value,dict):
        kind=str(value.get('type',value.get('kind',''))).lower()
        if (value.get('isSidechain') is True or value.get('isSubagent') is True
            or str(value.get('role','')).lower() in ('tool','toolresult')
            or kind in ('tool_result','toolresult','tool.execution_complete','function_call_output','custom_tool_call_output','progress','agent_progress')):
            return found
        cid=value.get('call_id') or value.get('toolCallId') or value.get('id')
        function=value.get('function') if isinstance(value.get('function'),dict) else {}
        name=value.get('name') or function.get('name') or value.get('toolName') or value.get('tool')
        if cid and (kind in ('tool_use','toolcall','function_call','custom_tool_call','commandexecution','filechange','mcptoolcall','dynamictoolcall')
                    or 'arguments' in value or 'arguments' in function):
            args=value.get('arguments',function.get('arguments',value.get('input',value.get('changes',{'command':value.get('command')}))))
            if kind=='commandexecution':name=name or 'shell'
            found[str(cid)]={'name':name,'input':deepcopy(parse_arguments(args))}
        for key in ('message','content','payload','item','data','toolRequests','toolCalls','requests','response','metadata','toolCallRounds','request'):
            if key in value:calls_from(value[key],found)
        if isinstance(value.get('result'),dict):calls_from(value['result'].get('metadata',{}),found)
    return found


def time_for(message,raw,info,source):
    value=message.get('timestamp')
    if isinstance(value,int) and not isinstance(value,bool):
        return value,message.get('timestampSource') or ('request' if source=='vscode_chat' else 'source')
    for value,unit in ((raw.get('timestamp'),None),((raw.get('message') or {}).get('timestamp') if isinstance(raw.get('message'),dict) else None,'ms'),
                       ((raw.get('payload') or {}).get('completed_at_ms') if isinstance(raw.get('payload'),dict) else None,'ms')):
        parsed=timestamp_ms(value,unit)
        if parsed is not None:return parsed,'source'
    # Explicitly label the fallback, never pretend file time was an event time.
    return int(info['modified']),'file_mtime'


def request_for(document,message,scope_context):
    index=(message.get('sourceLocator') or {}).get('requestIndex')
    if index is None:return None
    value=document.scope_states.get(f'request:{index}',scope_context.get(f'request:{index}'))
    return value.get('request') if isinstance(value,dict) else None


def result_flag(source,message,block,raw,request=None):
    cid=block.get('tool_use_id') or message.get('toolCallId')
    if source=='claude':
        for b in (raw.get('message') or {}).get('content',[]) if isinstance((raw.get('message') or {}).get('content'),list) else []:
            if isinstance(b,dict) and b.get('type')=='tool_result' and b.get('tool_use_id')==cid:
                # Anthropic's optional is_error defaults to false.
                return not b.get('is_error',False)
    if source=='copilot_cli' and raw.get('type')=='tool.execution_complete':
        value=(raw.get('data') or {}).get('success')
        return value if isinstance(value,bool) else None
    if source=='pi':
        native=raw.get('message') or {}
        if native.get('role') in ('toolResult','bashExecution'):
            value=flags(native)
            if value is not None:return value
            return None
    if source=='vscode_chat' and isinstance(request,dict):
        meta=(request.get('result') or {}).get('metadata',{}) if isinstance(request.get('result'),dict) else {}
        results=meta.get('toolCallResults',{}) if isinstance(meta,dict) else {}
        if isinstance(results,dict):
            key=(message.get('sourceLocator') or {}).get('metadataToolCallResultKey')
            candidates=[v for k,v in results.items() if k in (cid,key) or k.rsplit('__vscode-',1)[0]==cid]
            if len(candidates)==1:
                value=flags(candidates[0])
                if value is not None:return value
        for chunk in request.get('response',[]):
            if isinstance(chunk,dict) and chunk.get('toolCallId')==cid:
                for v in (chunk.get('toolSpecificData'),chunk.get('resultDetails')):
                    value=flags(v)
                    if value is not None:return value
        return None
    payload=raw.get('payload') or {}
    item=payload.get('item') if isinstance(payload,dict) else None
    for v in (item,payload,block):
        value=flags(v)
        if value is not None:return value
    if source=='codex' and isinstance(payload,dict):
        output=payload.get('output')
        if isinstance(output,str):
            try:value=flags(json.loads(output))
            except ValueError:value=None
            if value is not None:return value
            # Explicit native shell-wrapper exit code, not an "error" text guess.
            match=re.search(r'^Process exited with code (-?\d+)\s*$',output,re.M)
            if match:return int(match[1])==0
    return None


def original_text(document,message,raw,scope_context):
    if document.source=='vscode_chat':
        request=request_for(document,message,scope_context)
        if not isinstance(request,dict):return None
        if message.get('role')=='user':
            value=request.get('message') or {};value=value.get('text') if isinstance(value,dict) else value
            return [value] if isinstance(value,str) else None
        locator=message.get('sourceLocator') or {};indices=locator.get('chunkIndices')
        if indices is None:return None
        labels={item['chunkIndex']:item['label'] for item in message.get('inlineReferences',[])
                if isinstance(item,dict) and 'chunkIndex' in item and 'label' in item}
        response=request.get('response') or []
        if isinstance(response,str):response=[{'value':response}]
        parts=[]
        for index in indices:
            chunk=response[index] if 0<=index<len(response) else {}
            if not isinstance(chunk,dict):chunk={'value':chunk}
            if chunk.get('kind')=='inlineReference':fragment=labels.get(index,'[unresolved inline reference]')
            else:
                fragment=chunk.get('value',chunk.get('content',''))
                if isinstance(fragment,dict):fragment=fragment.get('value')
                if fragment is not None and not isinstance(fragment,str):fragment=None
            if fragment is not None:parts.append(fragment)
        return [''.join(parts)]
    native=raw.get('message') or {}
    if document.source=='copilot_cli':native=raw.get('data') or {}
    if document.source=='codex':
        payload=raw.get('payload') or {};native=payload.get('item') or payload
    value=native.get('content') if isinstance(native,dict) else None
    if isinstance(value,str):return [value]
    if isinstance(value,list):
        return [b['text'] for b in value if isinstance(b,dict) and b.get('type') in
                ('text','Text','input_text','output_text') and isinstance(b.get('text'),str)]
    return None


def is_internal(raw):
    kind=str(raw.get('type','')).lower();payload=raw.get('payload') or {}
    return (raw.get('isSidechain') is True or raw.get('isSubagent') is True
            or kind in ('progress','agent_progress','subagent_message')
            or isinstance(payload,dict) and (payload.get('channel') in ('analysis','reasoning') or
                isinstance(payload.get('item'),dict) and payload['item'].get('channel') in ('analysis','reasoning')))


def project(document,info,*,raw_context=None,scope_context=None):
    raw=dict(raw_context or {});raw.update(dict(document.records));scope_context=scope_context or {}
    cache={}
    def definitions():
        # Global fallback only; most calls are defined in their own record. A
        # lazy scan avoids walking huge snapshot/replay context on every batch.
        if 'value' not in cache:
            found=calls_from(list(raw.values()))
            for state in {**scope_context,**document.scope_states}.values():calls_from(state,found)
            cache['value']=found
        return cache['value']
    output={};positions={};scopes={}
    def add(original,content,kind,role,part,*,cid=None,success=None,index=None,scope=None):
        index=original['rawRecord'] if index is None else index
        native=raw.get(index,{})
        if is_internal(native):return
        timestamp,origin=time_for(original,native,info,document.source)
        if document.source=='vscode_chat' and origin=='request':
            request=request_for(document,original,scope_context)
            if request is not None and request.get('timestamp') is None:origin='session'
        mid=(f'{document.id}:tool-result:{cid}:0' if kind=='tool-result' else
             f'{document.id}:tool-use:{cid}:0' if kind=='tool-use' else
             original['id'] if document.metadata.get('contentPolicy')==POLICY and original['id'].endswith(':text:0') else f'{original["id"]}:{part}:0')
        value={'id':mid,'sessionId':document.id,'source':document.source,'type':kind,'role':role,
               'uuid':original.get('uuid',mid),'parentUuid':None,'timestamp':timestamp,'timestampUnit':'ms',
               'timestampSource':origin,'rawRecord':index,'message':{'role':role,'content':content}}
        if cid is not None:value['toolCallId']=str(cid)
        if kind=='tool-result':value['status']=status_name(success)
        output[mid]=value
        old_order=document.message_order.get(original['id'],(index,int(original['id'].rsplit(':',1)[-1]) if original['id'].rsplit(':',1)[-1].isdigit() else 0))
        rank=0 if part=='text' else 1 if part.startswith('call:') else 2
        positions[mid]=(old_order[0] if index==original['rawRecord'] else index,old_order[1],rank,part)
        scopes[mid]=scope or document.message_scopes.get(original['id'],(document.raw_scopes.get(index) or [f'record:{index}'])[0])
    for message in document.messages:
        native=raw.get(message['rawRecord'],{})
        if is_internal(native):continue
        content=message.get('message',{}).get('content',[])
        text=[{'type':'text','text':no_attachments(b.get('text',''))} for b in content
              if isinstance(b,dict) and b.get('type')=='text' and isinstance(b.get('text'),str)]
        if text:
            original=original_text(document,message,native,scope_context)
            if original is not None:text=[{'type':'text','text':no_attachments(t)} for t in original]
        if message.get('role')=='user':
            for block in content:
                if isinstance(block,dict) and block.get('type')=='unknown':
                    value=block.get('value')
                    original=native.get('data') if document.source=='copilot_cli' else native.get('message')
                    if document.source=='codex':
                        payload=native.get('payload') or {};original=payload.get('item') or payload
                    if isinstance(original,dict) and isinstance(original.get('content'),dict):value=original['content']
                    if isinstance(value,dict) and str(value.get('type',value.get('kind',''))).lower() in FILE_ATTACHMENTS:continue
                    cleaned=no_attachments(value)
                    if cleaned is not OMIT and cleaned not in (None,{},[]):
                        text.append({'type':'text','text':json.dumps(cleaned,ensure_ascii=False,separators=(',',':'))})
        text=[b for b in text if b['text']]
        if message.get('role') in ('user','assistant') and text:
            joined=''.join(b['text'] for b in text).lstrip()
            if not joined.startswith(('<local-command-caveat>','<local-command-stdout>','<command-name>')):
                add(message,text,message['role'],message['role'],'text')
        previous_call=None
        for number,block in enumerate(content):
            if not isinstance(block,dict):continue
            if block.get('type')=='tool_use':
                cid=str(block.get('id') or message.get('toolCallId') or f'event-{message.get("uuid",message["rawRecord"])}')
                previous_call=cid
                request=request_for(document,message,scope_context)
                exact=calls_from(request if request is not None else native)
                definition=exact[cid] if cid in exact else definitions().get(cid,{})
                args=no_attachments(definition.get('input',block.get('input',{})))
                if args is OMIT:args={}
                name=definition.get('name') or block.get('name') or 'tool'
                add(message,[{'type':'tool_use','id':cid,'name':name,'input':args}],'tool-use','assistant',f'call:{number}',cid=cid)
                if document.source=='vscode_chat' and isinstance(request,dict):
                    completed=any(isinstance(chunk,dict) and chunk.get('toolCallId')==cid and
                        (chunk.get('isComplete') is True or flags(chunk.get('toolSpecificData')) is not None)
                        for chunk in request.get('response',[]))
                    if completed:
                        success=result_flag(document.source,message,{'tool_use_id':cid},native,request)
                        add(message,[{'type':'tool_result','tool_use_id':cid,'success':success,'content':status_name(success)}],
                            'tool-result','tool',f'result:{number}',cid=cid,success=success)
                # Canonical Codex completion can update only the call message.
                for index in message.get('relatedRawRecords',[]):
                    record=raw.get(index,{})
                    payload=record.get('payload') or {};item=payload.get('item') or {}
                    if payload.get('type')=='item_completed' and item.get('type') in ('CommandExecution','McpToolCall','FileChange','DynamicToolCall'):
                        success=flags(item);pseudo={**message,'timestamp':timestamp_ms(record.get('timestamp') or payload.get('completed_at_ms'))}
                        add(pseudo,[{'type':'tool_result','tool_use_id':cid,'success':success,'content':status_name(success)}],
                            'tool-result','tool',f'result:{number}',cid=cid,success=success,index=index)
            elif block.get('type')=='tool_result':
                cid=str(block.get('tool_use_id') or message.get('toolCallId') or previous_call or f'event-{message.get("uuid",message["rawRecord"])}')
                success=result_flag(document.source,message,block,native,request_for(document,message,scope_context))
                add(message,[{'type':'tool_result','tool_use_id':cid,'success':success,'content':status_name(success)}],
                    'tool-result','tool',f'result:{number}',cid=cid,success=success)
    ordered=sorted(output,key=lambda mid:(positions[mid],mid));counts={};order={}
    for mid in ordered:
        major=positions[mid][0];minor=counts.get(major,0);counts[major]=minor+1;order[mid]=[major,minor]
    meta=metadata(document.metadata)
    if document.source=='vscode_chat':
        created=(document.sync_state.get('header') or {}).get('creationDate')
        if isinstance(created,(int,str)):meta['creationDate']=created
    models=sorted({m['model'] for m in document.messages if isinstance(m.get('model'),str)})
    providers=sorted({m['provider'] for m in document.messages if isinstance(m.get('provider'),str)})
    if models:meta['models']=sorted(set(meta.get('models',[]))|set(models))
    if providers:meta['providers']=sorted(set(meta.get('providers',[]))|set(providers))
    # No source engine header/carry/raw events or full request snapshots escape.
    cursor=(document.metadata.get('appendCheckpoint') or document.sync_state.get('cursor'))
    if cursor:
        cursor={k:deepcopy(cursor[k]) for k in ('offset','nextLine','fingerprint','prefixFingerprint') if k in cursor}
    progress={'format':'json' if info['path'].endswith('.json') else 'jsonl','cursor':cursor,
              'snapshotBytes':getattr(document.records,'byte_count',None) or document.sync_state.get('snapshotBytes') or (cursor or {}).get('offset')}
    return {'source':document.source,'external_id':document.external_id,'project':document.project,'name':document.name,
            'messages':[output[mid] for mid in ordered],'metadata':meta,'append_base':deepcopy(document.append_base),
            'sync_state':progress,'message_scopes':scopes,'message_order':order}


def content_digest(messages):
    values=[]
    for message in messages:
        value=deepcopy(message)
        value.pop('rawRecord',None)  # Internal source location is not analytical content.
        if value.get('timestampSource')=='file_mtime':value.pop('timestamp',None)
        values.append(value)
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
