from __future__ import annotations
import hashlib, hmac, json, tempfile, threading, time, unittest, urllib.error, urllib.request, uuid
from unittest.mock import patch
from pathlib import Path
from home_relay import HomeConfig, HomeServer
from home_agent import Journal, tick

class HomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = HomeConfig.for_test(Path(self.tmp.name) / 'home.sqlite')
        self.start()

    def start(self):
        self.server = HomeServer(('127.0.0.1', 0), self.cfg)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def tearDown(self):
        self.stop(); self.tmp.cleanup()

    def req(self, path, method='GET', obj=None, token=None, sign=False, nonce=None):
        body = b'' if obj is None else json.dumps(obj, separators=(',', ':')).encode()
        headers = {'Content-Type': 'application/json'}
        if token: headers['Authorization'] = 'Bearer ' + token
        if sign:
            ts = str(int(time.time())); nonce = nonce or uuid.uuid4().hex
            msg = f'{method}\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}'.encode()
            headers.update({'X-Device-Id':'device1','X-Timestamp':ts,'X-Nonce':nonce,
                            'X-Signature':hmac.new(b'command-secret', msg, hashlib.sha256).hexdigest()})
        req = urllib.request.Request(self.base + path, method=method, data=body if method != 'GET' else None, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as res: return res.status, json.load(res)
        except urllib.error.HTTPError as exc:
            with exc: return exc.code, json.load(exc)

    def command(self, request_id=None, expires=None, nonce=None):
        return self.req('/v1/home/commands', 'POST', {'request_id': request_id or str(uuid.uuid4()), 'operation':'preheat', 'expires_at': expires or int(time.time())+60}, sign=True, nonce=nonce)

    def test_state_scope_and_event_queue_are_independent(self):
        data={'quota': {'ok':True}, 'mac':{'online':True}, 'preheat':{'phase':'idle'}}
        self.assertEqual(self.req('/v1/home/state','PUT',data,token='agent-secret')[0],200)
        self.assertEqual(self.req('/v1/home/state',token='read-secret')[1]['data'],data)
        self.assertEqual(self.req('/v1/home/state',token='agent-secret')[0],401)
        self.assertEqual(self.req('/v1/events',token='read-secret')[0],404)
        self.assertEqual(self.req('/v1/home/commands','POST',{},token='read-secret')[0],401)
        self.assertEqual(self.req('/v1/home/state','PUT',{'wifi_password':'secret'},token='agent-secret')[0],400)

    def test_expiry_replay_and_idempotency_across_restart(self):
        rid=str(uuid.uuid4()); body={'request_id':rid,'operation':'preheat','expires_at':int(time.time())+60}
        self.assertEqual(self.req('/v1/home/commands','POST',body,sign=True,nonce='fixednonce123456')[0],202)
        self.assertEqual(self.req('/v1/home/commands','POST',body,sign=True,nonce='fixednonce123456')[0],409)
        self.stop(); self.start()
        self.assertEqual(self.req('/v1/home/commands','POST',body,sign=True,nonce='fixednonce123456')[0],409)
        code,result=self.req('/v1/home/commands','POST',body,sign=True)
        self.assertEqual((code,result['status']),(200,'pending'))
        self.assertEqual(self.command(expires=int(time.time())-1)[0],400)

    def test_concurrency_and_offline_expiry(self):
        rid=str(uuid.uuid4()); self.assertEqual(self.command(rid)[0],202)
        self.assertEqual(self.command()[0],429)
        with self.server.store.connect() as db: db.execute('UPDATE commands SET expires_at=? WHERE request_id=?',(int(time.time())-1,rid))
        code,result=self.req('/v1/home/commands/'+rid,token='read-secret')
        self.assertEqual(result['status'],'expired')
        self.assertEqual(self.req('/v1/home/agent/claim','POST',{},token='agent-secret')[1]['command'],None)

    def test_claim_crash_is_unknown_and_never_retried(self):
        rid=str(uuid.uuid4()); self.command(rid)
        self.assertEqual(self.req('/v1/home/agent/claim','POST',{},token='agent-secret')[1]['command']['request_id'],rid)
        self.stop(); self.start()
        with self.server.store.connect() as db: db.execute('UPDATE commands SET lease_until=? WHERE request_id=?',(int(time.time())-1,rid))
        self.assertEqual(self.req('/v1/home/agent/claim','POST',{},token='agent-secret')[1]['command'],None)
        self.assertEqual(self.req('/v1/home/commands/'+rid,token='read-secret')[1]['status'],'unknown')
        self.assertEqual(self.command()[0],429)
        self.assertTrue(self.server.store.resolve_unknown(rid,'reviewed'))
        self.assertEqual(self.req('/v1/home/commands/'+rid,token='read-secret')[1]['status'],'unknown')
        self.assertEqual(self.command()[0],202)

    def test_agent_result_and_state_stale(self):
        rid=str(uuid.uuid4()); self.command(rid)
        self.req('/v1/home/agent/claim','POST',{},token='agent-secret')
        body={'status':'completed','reason':'turn_completed','window':{'result':'reset_changed_unconfirmed'}}
        self.assertEqual(self.req('/v1/home/agent/commands/'+rid,'POST',body,token='agent-secret')[0],200)
        self.assertEqual(self.req('/v1/home/commands/'+rid,token='read-secret')[1]['window']['result'],'reset_changed_unconfirmed')
        self.req('/v1/home/state','PUT',{'quota':{},'mac':{},'preheat':{}},token='agent-secret')
        with self.server.store.connect() as db: db.execute('UPDATE home_state SET updated_at=?',(int(time.time())-200,))
        self.assertEqual(self.req('/v1/home/state',token='read-secret')[1]['freshness'],'stale')
        with self.server.store.connect() as db: db.execute('UPDATE home_state SET updated_at=?',(int(time.time())-1000,))
        self.assertEqual(self.req('/v1/home/state',token='read-secret')[1]['freshness'],'offline')

    def test_mock_end_to_end_publishes_and_completes(self):
        rid=str(uuid.uuid4()); self.command(rid)
        usage={'ok':True,'stale':False,'five_hour':{'used_percent':0,'reset_at_epoch':int(time.time())+600}}
        status={'remote_preheat_guard':1,'preheat_phase':'idle','cooldown_active':False,'failure_cooldown_remaining_seconds':0,'last_window_assessment':{'result':'reset_changed_unconfirmed'}}
        def local(path): return {'/usage':usage,'/health':{'mac_online':True},'/api/preheat/status':status}[path]
        with patch.dict('os.environ',{'HOME_ENABLE_PREHEAT':'1'}),patch('home_agent.read_local',side_effect=local),patch('home_agent.execute_preheat',return_value={'ok':True,'status':'completed'}) as execute:
            tick(self.base,'agent-secret',Journal(Path(self.tmp.name)/'journal.sqlite'))
        execute.assert_called_once()
        self.assertEqual(self.req('/v1/home/commands/'+rid,token='read-secret')[1]['status'],'completed')
        self.assertTrue(self.req('/v1/home/state',token='read-secret')[1]['data']['mac']['online'])

    def test_simultaneous_commands_allow_only_one(self):
        barrier=threading.Barrier(6); outcomes=[]
        def submit():
            barrier.wait(); outcomes.append(self.command()[0])
        workers=[threading.Thread(target=submit) for _ in range(6)]
        for thread in workers: thread.start()
        for thread in workers: thread.join()
        self.assertEqual(outcomes.count(202),1); self.assertEqual(outcomes.count(429),5)

if __name__ == '__main__': unittest.main()
