"""Transient source replay. Cloud stores cursors/digests, never native context."""
from copy import deepcopy
from . import incremental
from .sources.base import SourceFile
from .batching import PrefixPath


class Replay:
    def __init__(self):self.reset()

    def reset(self):
        self.path=None;self.tag=None;self.records={};self.raw_scopes={};self.scope_states={}
        self.messages={};self.scopes={};self.order={};self.state={};self.session=None;self.primary=None;self.invalid=False

    def context(self):
        if self.session is None:return None,None
        checkpoint={'path':self.primary['path'],'parserVersion':self.primary['parser_version'],'state':deepcopy(self.state)} if self.state else None
        return {'session':deepcopy(self.session),'primary':deepcopy(self.primary)},checkpoint

    def seed(self,adapter,entry,cloud):
        primary=(cloud.get('context') or {}).get('primary')
        if not primary:return
        tag=(str(entry.path),primary['fingerprint'],primary['parser_version'])
        if self.tag==tag:return
        self.reset();self.path=str(entry.path);self.tag=tag
        cursor=(cloud.get('checkpoint') or {}).get('state',{}).get('cursor')
        if not cursor or primary['parser_version']!=adapter.parser_version:
            self.invalid=True;return
        if entry.path.stat().st_size<cursor['offset']:self.invalid=True;return
        session=deepcopy(cloud['context']['session'])
        if adapter.source in ('claude','pi'):
            # Their analytical tail does not require old conversation payloads.
            self.primary=deepcopy(primary);self.session=session
            if adapter.source=='claude':
                session['metadata']['appendCheckpoint']={'offset':cursor['offset'],'nextLine':cursor['nextLine'],
                    'prefixFingerprint':cursor.get('prefixFingerprint',cursor.get('fingerprint')),'parserVersion':adapter.parser_version}
                session['metadata']['metadataOnly']=session.get('userMessageCount',0)==0
            else:
                version=session['metadata'].get('formatVersion')
                if version not in (1,2,3):self.session=None;self.invalid=True;return
                self.state={'kind':'pi','engineVersion':incremental.ENGINE_VERSION,'format':'jsonl','cursor':deepcopy(cursor),
                    'header':{'type':'session','id':session['externalId'],'version':version,'cwd':session['project']},
                    'contexts':{},'previous':session['metadata'].get('defaultLeaf'),'name':session['name'],'fallbackName':session['name']}
            return
        if entry.path.suffix!='.jsonl':self.invalid=True;return
        prefix=SourceFile(PrefixPath(entry.path,cursor['offset']),entry.project,entry.dependencies)
        doc=incremental.initialize(adapter,prefix,adapter.parse(prefix))
        if doc.records.hasher.hexdigest()!=cursor.get('fingerprint',cursor.get('prefixFingerprint')):
            self.invalid=True;return
        self.install(doc,primary['fingerprint'],adapter.parser_version,str(entry.path))

    def install(self,doc,fingerprint,parser,path):
        self.records=dict(doc.records);self.raw_scopes=deepcopy(doc.raw_scopes)
        self.scope_states=deepcopy(doc.scope_states);self.messages={m['id']:deepcopy(m) for m in doc.messages}
        self.scopes=deepcopy(doc.message_scopes);self.order=deepcopy(doc.message_order)
        self.state=deepcopy(doc.sync_state)
        self.session={'id':doc.id,'source':doc.source,'externalId':doc.external_id,'project':doc.project,
                      'name':doc.name,'metadata':deepcopy(doc.metadata),'messageCount':len(doc.messages)}
        self.primary={'path':path,'fingerprint':fingerprint,'parser_version':parser,'message_count':len(doc.messages)}
        self.path=path;self.tag=(path,fingerprint,parser);self.invalid=False

    def advance(self,doc,info):
        if not doc.append_base or self.session is None:
            self.install(doc,info['fingerprint'],info['parserVersion'],info['path']);return
        if doc.changed_scopes:
            remove=[mid for mid in self.messages if self.scopes.get(mid) in doc.changed_scopes]
            for mid in remove:self.messages.pop(mid,None);self.scopes.pop(mid,None);self.order.pop(mid,None)
        if doc.raw_snapshot_replace:self.records={};self.raw_scopes={}
        self.records.update(dict(doc.records));self.raw_scopes.update(deepcopy(doc.raw_scopes))
        for scope,value in doc.scope_states.items():
            if value is None:self.scope_states.pop(scope,None)
            else:self.scope_states[scope]=deepcopy(value)
        self.messages.update({m['id']:deepcopy(m) for m in doc.messages})
        self.scopes.update(deepcopy(doc.message_scopes));self.order.update(deepcopy(doc.message_order))
        self.state=deepcopy(doc.sync_state)
        self.session.update(name=doc.name,project=doc.project,metadata=deepcopy(doc.metadata),messageCount=len(self.messages))
        self.primary.update(path=info['path'],fingerprint=info['fingerprint'],parser_version=info['parserVersion'],message_count=len(self.messages))
        self.tag=(info['path'],info['fingerprint'],info['parserVersion'])

    def load_scope_state(self,sid,scope):return deepcopy(self.scope_states.get(scope))
    def load_scope_records(self,sid,scope):
        return [(index,deepcopy(self.records[index])) for index in sorted(self.records) if scope in self.raw_scopes.get(index,[])]
