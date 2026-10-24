"""Synthetic Docker/daemon smoke test. Never discovers real Agent directories.

Use only with an isolated local Compose project. May restart only explicitly
named claudeviewer-smoke* projects. No key/token values are printed or reported.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote,urlsplit
import uuid

import requests

ROOT=Path(__file__).resolve().parents[2]
COMPOSE=ROOT/'server/deploy/docker-compose.local.yml'


def wait(check,seconds=90):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            result=check()
            if result:return result
        except (requests.RequestException,AssertionError,KeyError,ValueError):pass
        time.sleep(.5)
    raise RuntimeError('Smoke check timed out (no credentials or payloads logged)')


def run(origin,owner_file,*,restart_project=None):
    parsed=urlsplit(origin)
    if parsed.scheme!='http' or parsed.hostname not in ('localhost','127.0.0.1'):
        raise ValueError('Smoke is restricted to an isolated loopback HTTP server')
    if restart_project and not restart_project.startswith('claudeviewer-smoke'):
        raise ValueError('Restart is allowed only for explicitly named claudeviewer-smoke* test projects')
    owner=Path(owner_file).read_text().strip();origin=origin.rstrip('/')
    web=requests.Session();web.trust_env=False
    def call(method,path,**kwargs):
        response=web.request(method,origin+path,timeout=10,**kwargs)
        response.raise_for_status();return response
    def login():
        page=call('GET','/auth/login')
        nonce=re.search(r'name="nonce" value="([^"]+)"',page.text).group(1)
        response=call('POST','/auth/login',data={'nonce':nonce,'key':owner},headers={'Origin':origin},allow_redirects=False)
        assert response.status_code==302
        page=call('GET','/')
        csrf=re.search(r'name="cloud-csrf" content="([^"]+)"',page.text).group(1)
        return {'Origin':origin,'X-CSRF-Token':csrf}
    wait(lambda:call('GET','/healthz').json().get('ok'))
    headers=login();checks=['health','owner login']
    device=call('POST','/api/devices',headers=headers,json={'name':'Docker synthetic smoke','control':True}).json()
    did=device['deviceId'];token=device['token']
    device_http=requests.Session();device_http.trust_env=False
    def machine(path,body=None):
        response=device_http.request('GET' if body is None else 'POST',origin+path,json=body,
            headers={'Authorization':'Bearer '+token},timeout=10)
        response.raise_for_status();return response.json()
    def dashboard():return next(d for d in call('GET','/api/devices').json() if d['id']==did)
    sid='docker-smoke-'+uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='viewer-docker-smoke-') as temp:
        temp=Path(temp);source=temp/'claude';source.mkdir();history=source/'history.jsonl'
        process_marker='SYNTHETIC_PROCESS_OMITTED_'+uuid.uuid4().hex
        rows=[{'type':'user','sessionId':sid,'uuid':'u','message':{'content':'Docker synthetic question'}},
              {'type':'assistant','uuid':'a','message':{'content':[{'type':'text','text':'Docker synthetic answer'},
                {'type':'tool_use','id':'call','name':'Agent','input':{'prompt':'Allowed synthetic task'}}]}},
              {'type':'progress','content':process_marker},
              {'type':'user','uuid':'result','message':{'content':[{'type':'tool_result','tool_use_id':'call','content':process_marker}]}}]
        history.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
        (source/'agent-child.jsonl').write_text(json.dumps({'type':'user','sessionId':'child','message':{'content':process_marker}})+'\n')
        roots=temp/'roots.json';roots.write_text(json.dumps({'claude':[str(source)]}))
        key=temp/'device.token';key.write_text(token);os.chmod(key,0o600)
        log=temp/'daemon.log'
        with log.open('wb') as output:
            process=subprocess.Popen([sys.executable,'-u','-m','deamon','--cloud',origin,
                '--token-file',str(key),'--roots-file',str(roots),'--quiet-seconds','0',
                '--batch-bytes','8192','--progress-query-files','2','--allow-http-loopback'],
                cwd=ROOT,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            wait(lambda:dashboard()['online'])
            assert call('GET','/api/sessions',params={'q':sid}).json()['total']==0
            checks.append('idle daemon does not scan')
            def sync_job():
                job=call('POST',f'/api/devices/{did}/jobs',headers={**headers,'Idempotency-Key':uuid.uuid4().hex},json={'sources':['claude']}).json()
                def finished():
                    value=dashboard().get('job')
                    if value and value['id']==job['id'] and value['state'] in ('FAILED','CANCELLED','EXPIRED'):
                        raise RuntimeError('Synthetic cloud task did not succeed')
                    return value and value['id']==job['id'] and value['state']=='SUCCEEDED'
                wait(finished)
            sync_job();checks.append('cloud command -> FIFO GET -> received ACK -> stateless daemon -> batch POST')
            task=dashboard()['job'];assert task['received_at']>0
            assert not any(entry['id']==task['id'] for entry in dashboard()['queue'])
            checks.append('received ACK dequeues pending entry; execution history remains')
            session=call('GET','/api/sessions',params={'q':sid}).json()['items'][0]
            assert session['messageCount']==4 and session['userMessageCount']==1
            cloud_sid=quote(session['id'],safe='')
            assert web.get(origin+f'/api/sessions/{cloud_sid}/records',timeout=10).status_code==410
            messages=call('GET',f'/api/sessions/{cloud_sid}/messages').json()
            assert 'Allowed synthetic task' in json.dumps(messages) and process_marker not in json.dumps(messages)
            checks.append('analysis only: timestamped prose/calls + outcome flags; no native records/processes')
            progress=machine('/sync/v1/progress/query',{'files':[{'source':'claude','path':str(history.resolve())}]})['files'][0]
            assert progress['checkpoint']['offset']==history.stat().st_size
            checks.append('progress owned by cloud')
            if restart_project:
                env={**os.environ,'VIEWER_LOCAL_PORT':str(parsed.port or 80)}
                result=subprocess.run(['docker','compose','-p',restart_project,'-f',str(COMPOSE),'restart','viewer'],
                    env=env,capture_output=True,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if result.returncode:raise RuntimeError('Restart of isolated smoke project failed')
                wait(lambda:call('GET','/healthz').json().get('ok'))
                # Existing browser cookie, owner key, device and checkpoint survive.
                assert call('GET','/api/sources').status_code==200
                assert machine('/sync/v1/capabilities')['deviceId']==did
                after=machine('/sync/v1/progress/query',{'files':[{'source':'claude','path':str(history.resolve())}]})['files'][0]
                assert after==progress
                checks.append('container restart preserves cookies/device/cloud cursor')
            with history.open('a') as stream:
                stream.write(json.dumps({'type':'user','sessionId':sid,'uuid':'later','message':{'content':'Synthetic incremental followup'}})+'\n')
            sync_job()
            assert call('GET',f'/api/sessions/{cloud_sid}/messages').json()['total']==5
            checks.append('incremental followup without duplicate messages')
            assert not list(temp.rglob('*.db')) and not list(temp.rglob('*checkpoint*'))
            checks.append('no local daemon progress files')
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
    return {'ok':True,'origin':origin,'checks':checks,'syntheticOnly':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin',default='http://localhost:41881')
    parser.add_argument('--owner-key-file',required=True)
    parser.add_argument('--restart-project')
    parser.add_argument('--report')
    args=parser.parse_args();result=run(args.origin,args.owner_key_file,restart_project=args.restart_project)
    rendered=json.dumps(result,indent=2)
    if args.report:
        path=Path(args.report);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(rendered+'\n')
    print(rendered)


if __name__=='__main__':main()
