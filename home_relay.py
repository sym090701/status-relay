#!/usr/bin/env python3
"""Isolated XiaoMiao state/command service. Run behind a path-restricted TLS proxy."""
from __future__ import annotations
import hashlib, hmac, json, os, re, sqlite3, threading, time, uuid
from contextlib import closing, contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MAX_STATE = 8192
MAX_COMMAND_TTL = 300
MAX_CLOCK_SKEW = 60
LEASE_SECONDS = 300
STATE_STALE_SECONDS = 120
ALLOWED_FIELDS = {
    'quota': {'ok','stale','server_updated_epoch','five_hour','weekly'},
    'mac': {'online','updated_at','cpu_percent','memory_percent'},
    'preheat': {'phase','last_result','cooldown_active','cooldown_remaining_seconds',
                'failure_cooldown_remaining_seconds','last_turn_at','last_window_assessment'},
    'window': {'reset_at_epoch','remaining_percent','used_percent','reset_after_seconds'},
    'assessment': {'result','before_reset','after_reset','before_remaining','after_remaining'},
}

def valid_state(state):
    if not isinstance(state, dict) or set(state) != {'quota','mac','preheat'}: return False
    for group, value in state.items():
        if not isinstance(value, dict) or set(value) - ALLOWED_FIELDS[group]: return False
        for k,v in value.items():
            if k in ('five_hour','weekly'):
                if v is not None and (not isinstance(v,dict) or set(v)-ALLOWED_FIELDS['window'] or any(isinstance(x,(dict,list)) for x in v.values())): return False
            elif k == 'last_window_assessment':
                if v is not None and (not isinstance(v,dict) or set(v)-ALLOWED_FIELDS['assessment'] or any(isinstance(x,(dict,list)) for x in v.values())): return False
            elif isinstance(v,(dict,list)): return False
            if isinstance(v,str) and len(v)>128: return False
    return True

class HomeConfig:
    def __init__(self):
        self.db = Path(os.environ['HOME_RELAY_DB'])
        self.read_token = os.environ['HOME_READ_TOKEN']
        self.agent_token = os.environ['HOME_AGENT_TOKEN']
        self.device_id = os.environ['HOME_DEVICE_ID']
        self.device_secret = os.environ['HOME_DEVICE_SECRET']
        if min(map(len,(self.read_token,self.agent_token,self.device_secret))) < 32:
            raise ValueError('home credentials must be at least 32 characters')
        if len({self.read_token,self.agent_token,self.device_secret}) != 3: raise ValueError('credentials must be distinct')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',self.device_id): raise ValueError('invalid device id')
    @classmethod
    def for_test(cls,path):
        obj=object.__new__(cls); obj.db=path; obj.read_token='read-secret'; obj.agent_token='agent-secret'; obj.device_id='device1'; obj.device_secret='command-secret'; return obj

class Store:
    def __init__(self,path):
        self.path=path; path.parent.mkdir(parents=True,exist_ok=True); self.lock=threading.RLock()
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS home_state (id INTEGER PRIMARY KEY CHECK(id=1),updated_at INTEGER NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS commands (request_id TEXT PRIMARY KEY,device_id TEXT NOT NULL,expires_at INTEGER NOT NULL,created_at INTEGER NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,lease_until INTEGER,window TEXT);
                CREATE TABLE IF NOT EXISTS nonces (device_id TEXT NOT NULL,nonce TEXT NOT NULL,expires_at INTEGER NOT NULL,PRIMARY KEY(device_id,nonce));
                CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL,at INTEGER NOT NULL,source TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,window TEXT);
            ''')
    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path,timeout=10)) as db:
            db.row_factory=sqlite3.Row
            with db: yield db
    @staticmethod
    def audit(db,rid,source,status,reason,window=None):
        db.execute('INSERT INTO audit(request_id,at,source,status,reason,window) VALUES(?,?,?,?,?,?)',(rid,int(time.time()),source,status,reason,json.dumps(window) if window else None))
    def expire(self,db,now):
        rows=db.execute("SELECT request_id,status FROM commands WHERE (status='pending' AND expires_at<=?) OR (status='running' AND lease_until<=?)",(now,now)).fetchall()
        for row in rows:
            status,reason=('expired','agent_offline_or_command_expired') if row['status']=='pending' else ('unknown','agent_result_not_confirmed')
            db.execute('UPDATE commands SET status=?,reason=? WHERE request_id=?',(status,reason,row['request_id']))
            self.audit(db,row['request_id'],'relay',status,reason)
    def accept_nonce(self,device,nonce,expiry):
        with self.lock,self.connect() as db:
            db.execute('DELETE FROM nonces WHERE expires_at<?',(int(time.time()),))
            try: db.execute('INSERT INTO nonces VALUES(?,?,?)',(device,nonce,expiry)); return True
            except sqlite3.IntegrityError: return False
    def create(self,rid,device,expires):
        now=int(time.time())
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db,now)
            old=db.execute('SELECT * FROM commands WHERE request_id=?',(rid,)).fetchone()
            if old:
                if old['device_id']!=device or old['expires_at']!=expires: return 409,{'error':'request_id_conflict'}
                return 200,self.view(old)
            if not now < expires <= now + MAX_COMMAND_TTL: return 400,{'error':'invalid_expiry'}
            if db.execute("SELECT COUNT(*) FROM commands WHERE status IN ('pending','running') OR (status='unknown' AND reason NOT GLOB 'operator_resolved:*')").fetchone()[0]: return 429,{'error':'command_in_progress_or_unknown'}
            if db.execute('SELECT COUNT(*) FROM commands WHERE device_id=? AND created_at>?',(device,now-60)).fetchone()[0]>=5: return 429,{'error':'rate_limited'}
            db.execute('INSERT INTO commands VALUES(?,?,?,?,?,?,?,?)',(rid,device,expires,now,'pending','accepted',None,None)); self.audit(db,rid,device,'accepted','accepted'); self.audit(db,rid,'relay','pending','awaiting_agent')
            return 202,{'request_id':rid,'status':'pending','reason':'accepted','expires_at':expires}
    @staticmethod
    def view(row):
        return {'request_id':row['request_id'],'status':row['status'],'reason':row['reason'],'expires_at':row['expires_at'],'window':json.loads(row['window']) if row['window'] else None}
    def status(self,rid):
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db,int(time.time())); row=db.execute('SELECT * FROM commands WHERE request_id=?',(rid,)).fetchone(); return self.view(row) if row else None
    def claim(self):
        now=int(time.time())
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db,now)
            if db.execute("SELECT 1 FROM commands WHERE status='running'").fetchone(): return None
            row=db.execute("SELECT * FROM commands WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if not row: return None
            db.execute("UPDATE commands SET status='running',reason='claimed',lease_until=? WHERE request_id=?",(now+LEASE_SECONDS,row['request_id']))
            self.audit(db,row['request_id'],'home-agent','running','claimed')
            return {'request_id':row['request_id'],'operation':'preheat','expires_at':row['expires_at']}
    def result(self,rid,status,reason,window):
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db,int(time.time())); row=db.execute('SELECT * FROM commands WHERE request_id=?',(rid,)).fetchone()
            if not row or row['status']!='running': return False
            db.execute('UPDATE commands SET status=?,reason=?,window=? WHERE request_id=?',(status,reason,json.dumps(window) if window else None,rid)); self.audit(db,rid,'home-agent',status,reason,window); return True
    def resolve_unknown(self,rid,reason):
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status FROM commands WHERE request_id=?',(rid,)).fetchone()
            if not row or row['status']!='unknown': return False
            db.execute("UPDATE commands SET reason=? WHERE request_id=?",('operator_resolved:'+reason,rid))
            self.audit(db,rid,'local-operator','unknown','operator_resolved:'+reason)
            return True

class HomeHandler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args): pass
    def send(self,code,obj):
        raw=json.dumps(obj,separators=(',',':'),allow_nan=False).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(raw)
    def auth(self,expected):
        given=self.headers.get('Authorization',''); return hmac.compare_digest(given.encode(),('Bearer '+expected).encode())
    def body(self):
        try: n=int(self.headers.get('Content-Length','-1'))
        except ValueError: return None
        if n<0 or n>MAX_STATE: return None
        b=self.rfile.read(n); return b if len(b)==n else None
    def signature(self,path,body):
        device=self.headers.get('X-Device-Id',''); nonce=self.headers.get('X-Nonce',''); ts=self.headers.get('X-Timestamp',''); sig=self.headers.get('X-Signature','')
        if device!=self.server.cfg.device_id or not re.fullmatch(r'[A-Za-z0-9_-]{16,96}',nonce) or not re.fullmatch(r'[0-9]{1,12}',ts) or not re.fullmatch(r'[0-9a-f]{64}',sig): return 401
        now=int(time.time()); stamp=int(ts)
        if abs(now-stamp)>MAX_CLOCK_SKEW: return 401
        msg=f'POST\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}'.encode()
        expected=hmac.new(self.server.cfg.device_secret.encode(),msg,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,sig): return 401
        if not self.server.store.accept_nonce(device,nonce,stamp+MAX_CLOCK_SKEW+1): return 409
        return 200
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/healthz': return self.send(200,{'ok':True})
        if path=='/v1/home/state' or path.startswith('/v1/home/commands/'):
            if not self.auth(self.server.cfg.read_token): return self.send(401,{'error':'unauthorized'})
            if path=='/v1/home/state':
                with self.server.store.connect() as db: row=db.execute('SELECT * FROM home_state WHERE id=1').fetchone()
                if not row: return self.send(200,{'freshness':'offline','updated_at':None,'data':None})
                age=max(0,int(time.time())-row['updated_at']); freshness='fresh' if age<=STATE_STALE_SECONDS else ('stale' if age<=600 else 'offline'); return self.send(200,{'freshness':freshness,'updated_at':row['updated_at'],'age_seconds':age,'data':json.loads(row['data'])})
            rid=path.removeprefix('/v1/home/commands/'); item=self.server.store.status(rid)
            return self.send(200,item) if item else self.send(404,{'error':'not_found'})
        return self.send(404,{'error':'not_found'})
    def do_PUT(self):
        if self.path!='/v1/home/state': return self.send(404,{'error':'not_found'})
        if not self.auth(self.server.cfg.agent_token): return self.send(401,{'error':'unauthorized'})
        raw=self.body()
        try: data=json.loads(raw or b''); assert valid_state(data); encoded=json.dumps(data,separators=(',',':'),allow_nan=False)
        except (ValueError,TypeError,AssertionError): return self.send(400,{'error':'invalid_state'})
        with self.server.store.lock,self.server.store.connect() as db: db.execute('INSERT INTO home_state VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,data=excluded.data',(int(time.time()),encoded))
        return self.send(200,{'ok':True})
    def do_POST(self):
        path=self.path
        if path not in ('/v1/home/commands','/v1/home/agent/claim') and not path.startswith('/v1/home/agent/commands/'): return self.send(404,{'error':'not_found'})
        raw=self.body()
        if raw is None: return self.send(400,{'error':'invalid_body'})
        if path=='/v1/home/commands':
            check=self.signature(path,raw)
            if check!=200: return self.send(check,{'error':'invalid_or_replayed_signature'})
        elif not self.auth(self.server.cfg.agent_token): return self.send(401,{'error':'unauthorized'})
        try: data=json.loads(raw)
        except (ValueError,UnicodeDecodeError): return self.send(400,{'error':'invalid_json'})
        if not isinstance(data,dict): return self.send(400,{'error':'invalid_request'})
        if path=='/v1/home/commands':
            try:
                rid=data['request_id']; exp=data['expires_at']; assert set(data)=={'request_id','operation','expires_at'} and data['operation']=='preheat' and str(uuid.UUID(rid))==rid and type(exp)==int
            except (KeyError,ValueError,TypeError,AttributeError,AssertionError): return self.send(400,{'error':'invalid_command'})
            code,result=self.server.store.create(rid,self.server.cfg.device_id,exp); return self.send(code,result)
        if path=='/v1/home/agent/claim':
            if data: return self.send(400,{'error':'invalid_request'})
            return self.send(200,{'command':self.server.store.claim()})
        rid=path.removeprefix('/v1/home/agent/commands/'); status=data.get('status'); reason=data.get('reason'); window=data.get('window')
        if set(data)-{'status','reason','window'} or status not in ('completed','failed','unknown') or not isinstance(reason,str) or not re.fullmatch(r'[a-z0-9_]{1,64}',reason) or (window is not None and (not isinstance(window,dict) or set(window)-ALLOWED_FIELDS['assessment'] or any(isinstance(x,(dict,list)) for x in window.values()))): return self.send(400,{'error':'invalid_result'})
        return self.send(200,{'ok':True}) if self.server.store.result(rid,status,reason,window) else self.send(409,{'error':'command_not_running'})

class HomeServer(ThreadingHTTPServer):
    request_queue_size=64
    def __init__(self,address,cfg): super().__init__(address,HomeHandler); self.cfg=cfg; self.store=Store(cfg.db)

if __name__=='__main__':
    cfg=HomeConfig(); server=HomeServer((os.environ.get('HOME_RELAY_BIND','127.0.0.1'),int(os.environ.get('HOME_RELAY_PORT','8781'))),cfg)
    try: server.serve_forever()
    finally: server.server_close()
