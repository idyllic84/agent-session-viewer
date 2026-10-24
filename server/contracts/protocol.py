"""Analysis contracts shared by the local and cloud Web Servers.

* ``packet``/``decode``: the local importer's internal, analysis-only projection
  of one source-file increment (validated before it touches the local store).
* ``check_message``/``check_session``/``check_ingest``: the cloud ingest change
  stream (protocol 6). Only four analytical message kinds and session metadata.
"""
import hashlib
from copy import deepcopy
import json
from .models import SessionDocument
from .session_policy import is_subagent as _is_subagent
from .analysis import POLICY,project,no_attachments,status_name,metadata,MESSAGE_KEYS

VERSION=5          # local importer packet format
INGEST_VERSION=6   # local -> cloud change stream
INGEST_OPS=('session','message','delete_message')
SESSION_KEYS={'id','source','externalId','project','name','lastModified','available','metadata'}
SOURCES={'claude':'Claude Code','pi':'pi','codex':'Codex','copilot_cli':'Copilot CLI','vscode_chat':'VS Code Chat'}
ENCODER=json.JSONEncoder(ensure_ascii=False,separators=(',',':'),allow_nan=False)
DOC_KEYS={'source','external_id','project','name','messages','metadata','append_base','sync_state','message_scopes','message_order'}
ROOT_KEYS={'version','file','document','changedScopes','instance','jobId','workerId','contentPolicy'}
PROGRESS_KEYS={'format','cursor','snapshotBytes'}
CURSOR_KEYS={'offset','nextLine','fingerprint','prefixFingerprint'}


class Problem(Exception):
    def __init__(self,code,message,status=409):super().__init__(message);self.code,self.status=code,status


def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def integer(value):return isinstance(value,int) and not isinstance(value,bool) and value>=0


def is_subagent(source,path,metadata=None,header=None):
    return _is_subagent(source,str(path).replace('\\','/'),metadata,header)


def require(condition,message,code='INVALID_PAYLOAD',status=422):
    if not condition:raise Problem(code,message,status)


def packet(document,info,*,raw_context=None,scope_context=None):
    return {'version':VERSION,'contentPolicy':POLICY,'file':deepcopy(info),
            'document':project(document,info,raw_context=raw_context,scope_context=scope_context),
            'changedScopes':sorted(document.changed_scopes)}


def encode_batch(value):return ENCODER.encode(value).encode('utf-8')


def decode(value):
    require(isinstance(value,dict) and value.get('version')==VERSION,'Unsupported protocol','PROTOCOL_VERSION',426)
    require(set(value)<=ROOT_KEYS and value.get('contentPolicy')==POLICY,'Analysis-only content policy required','CONTENT_POLICY')
    info=value.get('file');data=value.get('document')
    require(isinstance(info,dict) and set(info)=={'path','fingerprint','modified','parserVersion'},'Invalid file descriptor')
    require(isinstance(data,dict) and set(data)==DOC_KEYS,'Only analytical messages and progress may be transmitted','CONTENT_POLICY')
    require(data.get('source') in SOURCES and isinstance(data.get('external_id'),str) and 0<len(data['external_id'])<=512,'Invalid session identity')
    require(all(isinstance(info[k],str) and 0<len(info[k])<=8192 for k in ('path','fingerprint')),'Invalid file identity')
    require(all(integer(info[k]) for k in ('modified','parserVersion')),'Invalid source time/parser version')
    require(all(isinstance(data[k],str) for k in ('project','name')),'Invalid session text')
    require(isinstance(data['metadata'],dict) and metadata(data['metadata'])==data['metadata'],'Invalid session metadata','CONTENT_POLICY')
    require(not is_subagent(data['source'],info['path'],data['metadata']),'Subagent sessions are excluded','SUBAGENT_EXCLUDED')
    progress=data['sync_state']
    require(isinstance(progress,dict) and set(progress)==PROGRESS_KEYS,'Minimal source progress required','CONTENT_POLICY')
    require(progress['format'] in ('json','jsonl') and (progress['snapshotBytes'] is None or integer(progress['snapshotBytes'])),'Invalid source progress')
    cursor=progress['cursor']
    if cursor is not None:
        require(isinstance(cursor,dict) and set(cursor)<=CURSOR_KEYS and integer(cursor.get('offset')) and integer(cursor.get('nextLine')),'Invalid source cursor')
        require(any(isinstance(cursor.get(k),str) and len(cursor[k])==64 for k in ('fingerprint','prefixFingerprint')),'Missing prefix digest')
    require(isinstance(data['append_base'],dict) and set(data['append_base'])<={'path','fingerprint','parserVersion','messageCount'},'Invalid append base')
    require(isinstance(data['messages'],list),'Invalid messages')
    sid=data['source']+':'+data['external_id'];seen=set()
    for message in data['messages']:check_message(message,sid,data['source'],seen)
    for key in ('message_scopes','message_order'):
        require(isinstance(data[key],dict) and set(data[key])==seen,'Message scope/order must cover exactly the supplied messages')
    require(all(isinstance(scope,str) and 0<len(scope)<=1024 for scope in data['message_scopes'].values()),'Invalid scopes')
    require(all(isinstance(o,list) and len(o)==2 and all(integer(n) for n in o) for o in data['message_order'].values()),'Invalid ordering')
    changed=value.get('changedScopes');require(isinstance(changed,list) and all(isinstance(s,str) and 0<len(s)<=1024 for s in changed),'Invalid scope replacements')
    doc=SessionDocument(data['source'],data['external_id'],data['project'],[],name=data['name'],messages=deepcopy(data['messages']),metadata=deepcopy(data['metadata']))
    doc.append_base=deepcopy(data['append_base']);doc.sync_state=deepcopy(progress)
    doc.message_scopes=deepcopy(data['message_scopes']);doc.message_order=deepcopy(data['message_order']);doc.changed_scopes=set(changed)
    return doc,info


def check_message(message,sid,source,seen=None):
    """One analytical message: exactly user/assistant prose, invocation or outcome."""
    require(isinstance(message,dict) and set(message)<=MESSAGE_KEYS,'Unexpected message detail','CONTENT_POLICY')
    mid=message.get('id');kind=message.get('type');role=message.get('role')
    require(isinstance(mid,str) and mid.startswith(sid+':') and len(mid)<=2048 and (seen is None or mid not in seen),'Invalid/duplicate message ID')
    if seen is not None:seen.add(mid)
    require(message.get('sessionId')==sid and message.get('source')==source,'Message identity mismatch')
    require(kind in ('user','assistant','tool-use','tool-result'),'Only four analytical message kinds are permitted','CONTENT_POLICY')
    require(role==('assistant' if kind=='tool-use' else 'tool' if kind=='tool-result' else kind),'Invalid role')
    require(integer(message.get('rawRecord')),'Invalid source ordinal')
    require(isinstance(message.get('timestamp'),int) and not isinstance(message['timestamp'],bool) and message.get('timestampUnit')=='ms','Every message requires a timestamp')
    require(message.get('timestampSource') in ('source','request','session','file_mtime'),'Timestamp origin required')
    body=message.get('message');require(isinstance(body,dict) and set(body)=={'role','content'} and body['role']==role,'Invalid message body')
    blocks=body['content'];require(isinstance(blocks,list) and blocks,'Empty message')
    if kind in ('user','assistant'):
        require(all(isinstance(b,dict) and set(b)=={'type','text'} and b['type']=='text' and isinstance(b['text'],str) for b in blocks),'Only prose is allowed here','CONTENT_POLICY')
    elif kind=='tool-use':
        b=blocks[0]
        require(len(blocks)==1 and isinstance(b,dict) and set(b)=={'type','id','name','input'} and b['type']=='tool_use','Invalid invocation')
        require(isinstance(b['id'],str) and b['id']==message.get('toolCallId') and isinstance(b['name'],str),'Invalid call identity')
    else:
        b=blocks[0]
        require(len(blocks)==1 and isinstance(b,dict) and set(b)=={'type','tool_use_id','success','content'} and b['type']=='tool_result','Only a success/failure summary is permitted','CONTENT_POLICY')
        require(b['success'] is None or isinstance(b['success'],bool),'Invalid outcome flag')
        require(b['content']==status_name(b['success'])==message.get('status') and b['tool_use_id']==message.get('toolCallId'),'Outcome must not contain output/error details','CONTENT_POLICY')
    require(no_attachments(blocks)==blocks,'Attachments are not uploaded','CONTENT_POLICY')
    return message


def check_session(value,sid):
    """Session metadata row of the ingest stream. No local path/cursor/raw data."""
    require(isinstance(value,dict) and set(value)==SESSION_KEYS,'Invalid session change','CONTENT_POLICY')
    require(value['source'] in SOURCES and isinstance(value['externalId'],str) and 0<len(value['externalId'])<=512,'Invalid session identity')
    require(value['id']==sid==value['source']+':'+value['externalId'],'Session identity mismatch')
    require(isinstance(value['project'],str) and len(value['project'])<=8192 and isinstance(value['name'],str) and len(value['name'])<=8192,'Invalid session text')
    require(integer(value['lastModified']) and isinstance(value['available'],bool),'Invalid session state')
    require(isinstance(value['metadata'],dict) and metadata(value['metadata'])==value['metadata'],'Invalid session metadata','CONTENT_POLICY')
    require(not is_subagent(value['source'],'',value['metadata']),'Subagent sessions are excluded','SUBAGENT_EXCLUDED')
    return value


def check_ingest(value,dataset):
    """Strict shape of one ordinary batch POST: contiguous, complete groups."""
    require(isinstance(value,dict) and set(value)=={'protocolVersion','datasetId','expectedSeq','throughSeq','groups'},'Invalid batch envelope')
    require(value['protocolVersion']==INGEST_VERSION,'Unsupported protocol','PROTOCOL_VERSION',426)
    require(value['datasetId']==dataset,'Batch dataset does not match this upload key binding','DATASET_MISMATCH',409)
    require(integer(value['expectedSeq']) and integer(value['throughSeq']) and value['throughSeq']>value['expectedSeq'],'Invalid sequence range')
    groups=value['groups'];require(isinstance(groups,list) and groups,'Empty batch')
    expected=value['expectedSeq']+1;changes=[]
    for group in groups:
        require(isinstance(group,dict) and set(group)=={'id','firstSeq','lastSeq','changes'} and isinstance(group['id'],str) and 0<len(group['id'])<=128,'Invalid change group')
        require(group['firstSeq']==expected and integer(group['lastSeq']) and group['lastSeq']>=group['firstSeq'],'Change sequence gap')
        require(isinstance(group['changes'],list) and len(group['changes'])==group['lastSeq']-group['firstSeq']+1,'Incomplete change group')
        for change in group['changes']:
            require(isinstance(change,dict) and set(change)=={'seq','op','sessionId','data'} and change['seq']==expected,'Change sequence gap')
            require(change['op'] in INGEST_OPS and isinstance(change['sessionId'],str) and 0<len(change['sessionId'])<=600,'Invalid change')
            sid=change['sessionId'];data=change['data']
            source=sid.split(':',1)[0];require(source in SOURCES,'Unknown source')
            if change['op']=='session':check_session(data,sid)
            elif change['op']=='message':
                require(isinstance(data,dict) and set(data)=={'message','order'},'Invalid message change','CONTENT_POLICY')
                require(isinstance(data['order'],list) and len(data['order'])==2 and all(integer(n) for n in data['order']),'Invalid ordering')
                check_message(data['message'],sid,source)
            else:
                require(isinstance(data,dict) and set(data)=={'id'} and isinstance(data['id'],str) and data['id'].startswith(sid+':'),'Invalid deletion')
            changes.append(change);expected+=1
    require(expected-1==value['throughSeq'],'Batch does not end at throughSeq')
    return changes
