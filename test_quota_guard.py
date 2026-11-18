"""Tests for the shipped guard patch against the available local quota source."""
import asyncio, importlib.util, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SOURCE=Path(os.environ.get('QUOTA_SOURCE_DIR','/nonexistent'))
PATCH=Path(__file__).parent/'deploy/quota-remote-guard.patch'

@unittest.skipUnless((SOURCE/'quota_server.py').exists(), 'quota source is not present')
class GuardTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        try: import aiohttp
        except ImportError: raise unittest.SkipTest('aiohttp is required for quota guard tests')
        cls.tmp=tempfile.TemporaryDirectory(); target=Path(cls.tmp.name)/'quota_server.py'; shutil.copy2(SOURCE/'quota_server.py',target)
        subprocess.run(['patch','-p1','-i',str(PATCH)],cwd=cls.tmp.name,check=True,stdout=subprocess.DEVNULL)
        sys.path.insert(0,str(SOURCE)); spec=importlib.util.spec_from_file_location('patched_quota_server',target)
        cls.module=importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.module)
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    async def test_guard_checks_active_window_under_lock(self):
        c=self.module.Collector('unused',60); c.payload={'ok':True,'five_hour':{'used_percent':0,'reset_at_epoch':2000}}; c.updated=900; c.failed=False
        c.preheater.state={}
        await c.preheater.lock.acquire()
        with patch.object(self.module.time,'time',return_value=920),patch.object(self.module.asyncio,'create_subprocess_exec') as spawn:
            task=asyncio.create_task(c.preheater.run(c,source='remote')); await asyncio.sleep(0)
            c.payload['five_hour']['used_percent']=5
            c.preheater.lock.release(); result=await task
        self.assertEqual(result['status'],'waiting_reset'); spawn.assert_not_called()
    async def test_remote_guard_fails_closed_for_stale_or_missing_usage(self):
        c=self.module.Collector('unused',60); c.payload={'ok':True,'five_hour':{}}; c.updated=900; c.failed=False; c.preheater.state={}
        with patch.object(self.module.time,'time',return_value=920),patch.object(self.module.asyncio,'create_subprocess_exec') as spawn:
            result=await c.preheater.run(c,source='remote')
        self.assertEqual(result['status'],'quota_unavailable'); spawn.assert_not_called()
        self.assertEqual(c.preheater.status()['remote_preheat_guard'],1)
    async def test_authenticated_remote_header_selects_guard(self):
        from aiohttp.test_utils import TestClient,TestServer
        c=self.module.Collector('unused',60)
        async def idle(): await asyncio.Future()
        with tempfile.TemporaryDirectory() as d:
            token=Path(d)/'token'; token.write_text('fixture-token')
            with patch.dict(os.environ,{'PREHEAT_TOKEN_FILE':str(token)}),patch.object(c,'poll',idle),patch.object(c.preheater,'run',new=AsyncMock(return_value={'ok':False,'status':'waiting_reset'})) as run:
                async with TestClient(TestServer(self.module.create_app(c))) as client:
                    headers={'Authorization':'Bearer fixture-token','X-Preheat-Guard':'1'}
                    response=await client.post('/api/preheat',headers=headers,json={})
                    self.assertEqual((await response.json())['status'],'waiting_reset')
                    run.assert_awaited_once_with(c,source='remote')
