import tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from home_agent import Journal, run_command, select_state

class AgentTests(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.journal=Journal(Path(self.tmp.name)/'agent.sqlite')
    def tearDown(self): self.tmp.cleanup()
    def good(self): return {'ok':True,'stale':False,'five_hour':{'reset_at_epoch':int(time.time())+600,'used_percent':0}}
    def status(self): return {'preheat_phase':'idle','cooldown_active':False,'failure_cooldown_remaining_seconds':0,'remote_preheat_guard':1}
    def test_active_window_is_refused_without_call(self):
        quota=self.good(); quota['five_hour']['used_percent']=1
        with patch.dict('os.environ',{'HOME_ENABLE_PREHEAT':'1'}),patch('home_agent.read_local',side_effect=[quota,self.status()]),patch('home_agent.execute_preheat') as exe:
            result=run_command({'request_id':str(uuid.uuid4()),'expires_at':int(time.time())+30},self.journal)
        self.assertEqual(result['reason'],'active_window'); exe.assert_not_called()
    def test_uncertain_execution_and_restart_never_reexecutes(self):
        rid=str(uuid.uuid4()); command={'request_id':rid,'expires_at':int(time.time())+60}
        with patch.dict('os.environ',{'HOME_ENABLE_PREHEAT':'1'}),patch('home_agent.read_local',side_effect=[self.good(),self.status(),self.good()]),patch('home_agent.execute_preheat',side_effect=TimeoutError):
            result=run_command(command,self.journal)
        self.assertEqual(result['status'],'unknown')
        with patch('home_agent.execute_preheat') as exe:
            again=run_command(command,Journal(self.journal.path))
        self.assertEqual(again['status'],'unknown'); exe.assert_not_called()
    def test_crash_before_result_is_unknown(self):
        rid=str(uuid.uuid4()); self.journal.begin(rid)
        self.assertEqual(Journal(self.journal.path).recover()[0][1]['status'],'unknown')
    def test_select_state_drops_credentials(self):
        result=select_state({'ok':True,'token':'nope','five_hour':{'used_percent':1,'password':'nope'}},{'mac_online':True},{'preheat_phase':'idle','thread_id':'secret'})
        self.assertNotIn('token',str(result)); self.assertNotIn('password',str(result)); self.assertNotIn('thread_id',str(result))
    def test_guard_missing_never_calls_preheat(self):
        status=self.status(); status.pop('remote_preheat_guard')
        with patch.dict('os.environ',{'HOME_ENABLE_PREHEAT':'1'}),patch('home_agent.read_local',side_effect=[self.good(),status]),patch('home_agent.execute_preheat') as execute:
            result=run_command({'request_id':str(uuid.uuid4()),'expires_at':int(time.time())+60},self.journal)
        self.assertEqual(result['reason'],'atomic_guard_unavailable'); execute.assert_not_called()

if __name__=='__main__': unittest.main()
