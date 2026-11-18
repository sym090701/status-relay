#!/usr/bin/env python3
"""Home-side outbound-only bridge. Real preheat is disabled unless explicitly enabled."""
from __future__ import annotations
import json, os, sqlite3, time, urllib.error, urllib.request
from contextlib import closing, contextmanager
from pathlib import Path

LOCAL_BASE = os.environ.get('HOME_QUOTA_URL','http://127.0.0.1:8765').rstrip('/')
TOKEN_FILE = os.environ.get('HOME_QUOTA_TOKEN_FILE','')
WINDOW_KEYS = ('reset_at_epoch','remaining_percent','used_percent','reset_after_seconds')
PREHEAT_KEYS = ('preheat_phase','last_result','cooldown_active','cooldown_remaining_seconds','failure_cooldown_remaining_seconds','last_turn_at','last_window_assessment')
ASSESSMENT_KEYS = ('result','before_reset','after_reset','before_remaining','after_remaining')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl): return None

def pick(obj,keys): return {k:obj[k] for k in keys if k in obj and not isinstance(obj[k],(dict,list))}
def select_state(usage,health,status):
    quota=pick(usage,('ok','stale','server_updated_epoch'))
    for name in ('five_hour','weekly'):
        if isinstance(usage.get(name),dict): quota[name]=pick(usage[name],WINDOW_KEYS)
    preheat=pick(status,PREHEAT_KEYS)
    if 'preheat_phase' in preheat: preheat['phase']=preheat.pop('preheat_phase')
    if isinstance(status.get('last_window_assessment'),dict): preheat['last_window_assessment']=pick(status['last_window_assessment'],ASSESSMENT_KEYS)
    return {'quota':quota,'mac':{'online':bool(health.get('mac_online',False))},'preheat':preheat}

def request(url,method='GET',data=None,token='',timeout=10,extra_headers=None):
    body=json.dumps(data,separators=(',',':')).encode() if data is not None else None
    headers={'Accept':'application/json'}
    if body is not None: headers['Content-Type']='application/json'
    if token: headers['Authorization']='Bearer '+token
    if extra_headers: headers.update(extra_headers)
    req=urllib.request.Request(url,data=body,headers=headers,method=method)
    with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(req,timeout=timeout) as res:
        data=res.read(65537)
        if len(data)>65536: raise ValueError('response_too_large')
        return json.loads(data)

def local_token(): return Path(TOKEN_FILE).read_text().strip() if TOKEN_FILE else ''
def read_local(path): return request(LOCAL_BASE+path,token=local_token(),timeout=5)
def execute_preheat(): return request(LOCAL_BASE+'/api/preheat','POST',{},token=local_token(),timeout=220,extra_headers={'X-Preheat-Guard':'1'})

class Journal:
    def __init__(self,path):
        self.path=path; path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db: db.execute('CREATE TABLE IF NOT EXISTS executions (request_id TEXT PRIMARY KEY,result TEXT)')
    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path,timeout=10)) as db:
            with db: yield db
    def begin(self,rid):
        with self.connect() as db:
            try: db.execute('INSERT INTO executions VALUES(?,NULL)',(rid,)); return True
            except sqlite3.IntegrityError: return False
    def get(self,rid):
        with self.connect() as db:
            row=db.execute('SELECT result FROM executions WHERE request_id=?',(rid,)).fetchone()
        if not row: return None
        return json.loads(row[0]) if row[0] else {'status':'unknown','reason':'agent_interrupted','window':None}
    def finish(self,rid,result):
        with self.connect() as db: db.execute('UPDATE executions SET result=? WHERE request_id=?',(json.dumps(result),rid))
    def recover(self):
        with self.connect() as db:
            rows=db.execute('SELECT request_id,result FROM executions').fetchall()
        return [(rid,json.loads(result) if result else {'status':'unknown','reason':'agent_interrupted','window':None}) for rid,result in rows]
    def delivered(self,rid):
        with self.connect() as db: db.execute('DELETE FROM executions WHERE request_id=?',(rid,))

def result(status,reason,window=None): return {'status':status,'reason':reason,'window':window}
def run_command(command,journal):
    rid=command['request_id']
    if not journal.begin(rid): return journal.get(rid)
    outcome=None
    try:
        if int(time.time())>=command['expires_at']: outcome=result('failed','expired_before_execution')
        elif os.environ.get('HOME_ENABLE_PREHEAT')!='1': outcome=result('failed','execution_disabled')
        else:
            usage=read_local('/usage'); status=read_local('/api/preheat/status')
            window=usage.get('five_hour') or {}; used=window.get('used_percent'); reset=window.get('reset_at_epoch')
            if status.get('remote_preheat_guard') != 1: outcome=result('failed','atomic_guard_unavailable')
            elif not usage.get('ok') or usage.get('stale') or type(used) not in (int,float) or type(reset) not in (int,float): outcome=result('failed','quota_unavailable')
            elif reset>time.time() and used>0: outcome=result('failed','active_window')
            elif status.get('preheat_phase')!='idle': outcome=result('failed','preheat_busy_or_unknown')
            elif status.get('cooldown_active') or status.get('failure_cooldown_remaining_seconds',0)>0: outcome=result('failed','cooldown')
            elif int(time.time())>=command['expires_at']: outcome=result('failed','expired_before_execution')
            else:
                try:
                    response=execute_preheat()
                except Exception:
                    response=None
                if not isinstance(response,dict):
                    observation={'result':'unknown','before_reset':window.get('reset_at_epoch'),'before_remaining':window.get('remaining_percent')}
                    try:
                        after=read_local('/usage').get('five_hour') or {}
                        observation.update(after_reset=after.get('reset_at_epoch'),after_remaining=after.get('remaining_percent'))
                    except (OSError,ValueError,KeyError,TypeError,urllib.error.URLError): pass
                    outcome=result('unknown','execution_result_unconfirmed',observation)
                elif response.get('ok') is True and response.get('status')=='completed':
                    try: after=read_local('/api/preheat/status'); assessment=after.get('last_window_assessment'); assessment=pick(assessment,ASSESSMENT_KEYS) if isinstance(assessment,dict) else {'result':'unknown'}
                    except Exception: assessment={'result':'unknown'}
                    outcome=result('completed','turn_completed',assessment)
                elif response.get('status') in ('cooldown','failure_cooldown','quota_unavailable','waiting_reset'): outcome=result('failed',response['status'])
                else: outcome=result('unknown','execution_result_unconfirmed')
    except (OSError,ValueError,KeyError,TypeError,urllib.error.URLError): outcome=result('failed','local_precheck_unavailable')
    journal.finish(rid,outcome); return outcome

def publish_result(base,token,journal,rid,outcome):
    try:
        request(base+'/v1/home/agent/commands/'+rid,'POST',outcome,token,timeout=10); journal.delivered(rid)
    except urllib.error.HTTPError as exc:
        if exc.code==409: journal.delivered(rid)
        else: raise

def tick(base,token,journal):
    for rid,outcome in journal.recover(): publish_result(base,token,journal,rid,outcome)
    try:
        state=select_state(read_local('/usage'),read_local('/health'),read_local('/api/preheat/status'))
        request(base+'/v1/home/state','PUT',state,token,timeout=10)
    except (OSError,ValueError,KeyError,urllib.error.URLError): pass
    cmd=request(base+'/v1/home/agent/claim','POST',{},token,timeout=10).get('command')
    if cmd:
        outcome=run_command(cmd,journal); publish_result(base,token,journal,cmd['request_id'],outcome)

if __name__=='__main__':
    base=os.environ['HOME_RELAY_URL'].rstrip('/'); token=os.environ['HOME_AGENT_TOKEN']; journal=Journal(Path(os.environ['HOME_AGENT_DB']))
    if not base.startswith('https://') and os.environ.get('HOME_ALLOW_PRIVATE_HTTP')!='1': raise SystemExit('HTTPS required unless explicitly using private transport')
    while True:
        try: tick(base,token,journal)
        except (OSError,ValueError,KeyError,urllib.error.URLError) as exc: print('home bridge failed:',type(exc).__name__,flush=True)
        time.sleep(max(5,int(os.environ.get('HOME_POLL_SECONDS','15'))))
