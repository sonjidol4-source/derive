"""LS 옵션 응답 지연·토큰 갱신·호출 제한을 실제 네트워크 없이 검증한다."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from common import config, stamp
from engine import Engine
from ls_feed import FeedStopped, LsFeed, RequestGate
from test_ls_option_retry import FakeRequests, Timeout

AT=stamp('2026-10-29T13:34:00+09:00')
CLOSE=stamp('2026-10-29T16:20:00+09:00')


class BlockingRequests(FakeRequests):
    def __init__(self,tr='t2301'):
        super().__init__();self.block_tr=tr
        self.entered=threading.Event();self.release=threading.Event();self.blocked=False
        self.future_requests=[]

    def post(self,url,timeout,json,headers):
        tr=headers['tr_cd'];block=json[tr+'InBlock']
        if tr=='t1602' and block.get('market')=='4':self.future_requests.append(block.copy())
        if tr==self.block_tr and not self.blocked and (tr!='t1602' or block.get('market') in ('5','6')):
            self.blocked=True;self.entered.set()
            if not self.release.wait(3):raise AssertionError('test did not release blocked request')
            raise Timeout('simulated option read timeout')
        return super().post(url,timeout,json,headers)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.cfg=config();self.cfg['ls'].update(program=False,investor_board=False,poll_sec=.02)
        self.engine=Engine(self.cfg,self.directory);self.stop=threading.Event()
        self.feed=LsFeed(self.cfg,self.engine,self.stop)
        self.feed.keys=('test-key','test-secret');self.feed.access='cached';self.feed.expires=1e18
        self.feed.gate=RequestGate(self.stop,interval=0)
        self.threads=[]

    def tearDown(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(),'test collector did not stop')
        self.engine.store.close();self.temp.cleanup()

    def prepare(self,requests):
        self.feed.ensure_deriv(requests).wait=lambda:None
        self.feed.ensure_option_deriv(requests).wait=lambda:None
        self.assertIsNot(self.feed.deriv,self.feed.option_deriv)

    def launch(self,fn):
        failures=[]
        def run():
            try:fn()
            except FeedStopped:pass
            except BaseException as exc:failures.append(exc)
        thread=threading.Thread(target=run);self.threads.append(thread);thread.start()
        return thread,failures

    def exercise_blocked_option(self,tr):
        requests=BlockingRequests(tr);self.prepare(requests);cycles=[]
        original=self.feed.cycle_log
        def record(started,lag,outcome):
            original(started,lag,outcome);cycles.append((lag,outcome))
            if len(cycles)==2:self.stop.set()
        self.feed.cycle_log=record
        with patch('ls_feed.now_kst',return_value=AT),patch('ls_feed.in_session',return_value=True):
            self.feed.start_options(requests);self.threads.append(self.feed.option_worker)
            self.assertTrue(requests.entered.wait(1))
            fast,errors=self.launch(lambda:self.feed.fast_loop(requests,*self.feed.keys))
            try:
                fast.join(1)
                self.assertFalse(fast.is_alive(),'option response blocked futures loop')
                self.assertEqual(len(requests.future_requests),4)
                self.assertEqual(len(cycles),2)
                self.assertEqual(errors,[])
                self.assertFalse(requests.release.is_set())
            finally:requests.release.set()
            self.feed.option_worker.join(1)
        self.assertFalse(self.feed.option_worker.is_alive())
        log=(self.directory/'ls_fast.log').read_text(encoding='utf-8')
        self.assertIn('시작간격=',log);self.assertIn('데이터지연=',log)

    def test_blocked_board_timeout_does_not_delay_two_futures_cycles(self):
        self.exercise_blocked_option('t2301')

    def test_blocked_option_investor_does_not_hold_futures_request_lock(self):
        self.exercise_blocked_option('t1602')

    def test_after_close_option_timeout_does_not_block_futures_or_close_queries(self):
        requests=BlockingRequests();self.prepare(requests)
        def close_tick(req,key,secret,at):
            self.feed.series(req,'cached','1');self.feed.series(req,'cached','2');self.stop.set()
        self.feed.close_tick=close_tick
        with patch('ls_feed.now_kst',return_value=CLOSE),patch('ls_feed.in_session',return_value=False):
            self.feed.start_options(requests);self.threads.append(self.feed.option_worker)
            self.assertTrue(requests.entered.wait(1))
            fast,errors=self.launch(lambda:self.feed.fast_loop(requests,*self.feed.keys))
            try:
                fast.join(1);self.assertFalse(fast.is_alive())
                self.assertEqual(len(requests.future_requests),2)
                self.assertTrue((self.directory/'snapshot_futures.json').exists())
                self.assertEqual(errors,[])
            finally:requests.release.set()
            self.feed.option_worker.join(1)
        self.assertFalse((self.directory/'snapshot_calls.json').exists())

    def test_stop_suppresses_followup_option_request_and_snapshot_write(self):
        requests=BlockingRequests();self.prepare(requests)
        worker,errors=self.launch(lambda:self.feed.options_due(requests,AT,clock=lambda:AT))
        self.assertTrue(requests.entered.wait(1));self.stop.set();requests.release.set();worker.join(1)
        self.assertEqual(errors,[])
        self.assertFalse(any(tr=='t1602' for tr,_ in requests.posts))
        self.assertFalse((self.directory/'snapshot_calls.json').exists())

    def test_feed_run_joins_option_worker_on_stop(self):
        requests=BlockingRequests();self.prepare(requests)
        def fast_loop(*args):
            try:self.assertTrue(requests.entered.wait(1))
            finally:
                self.stop.set();requests.release.set()
        self.feed.fast_loop=fast_loop
        with patch.dict('sys.modules',{'requests':requests}),patch('ls_feed.credentials',return_value=self.feed.keys), \
                patch('ls_feed.now_kst',return_value=AT),patch('ls_feed.in_session',return_value=True):
            self.feed.run()
        self.assertIsNotNone(self.feed.option_worker)
        self.assertFalse(self.feed.option_worker.is_alive())
        self.assertTrue(self.feed.worker_stop.is_set())

    def test_token_refresh_is_shared_and_late_401_does_not_clear_new_token(self):
        self.feed.access=None;entered=threading.Event();release=threading.Event();issued=[];results=[]
        class TokenRequests:
            def post(_,url,**kwargs):
                issued.append(url);entered.set()
                if not release.wait(2):raise AssertionError('token release missing')
                return type('Response',(),{'status_code':200,'json':lambda self:{'access_token':'new-token','expires_in':3600}})()
        requests=TokenRequests()
        a,ea=self.launch(lambda:results.append(self.feed.token(requests,*self.feed.keys)))
        self.assertTrue(entered.wait(1))
        b,eb=self.launch(lambda:results.append(self.feed.token(requests,*self.feed.keys)))
        release.set();a.join(1);b.join(1)
        self.assertEqual(len(issued),1);self.assertEqual(results,['new-token','new-token'])
        self.assertEqual(ea+eb,[])
        self.feed.invalidate_token('old-token');self.assertEqual(self.feed.access,'new-token')
        self.feed.invalidate_token('new-token');self.assertIsNone(self.feed.access)

    def test_after_close_successful_part_is_not_repeated_while_board_retries(self):
        requests=FakeRequests(fail_board=1);self.prepare(requests)
        with patch('ls_feed.time.monotonic',return_value=0),patch('ls_feed.now_kst',return_value=CLOSE):
            # options_due's clock is explicit here so the test has no dependency on wall-clock date.
            original=self.feed.options_due
            self.feed.options_due=lambda req,at,**kw:original(req,at,clock=lambda:CLOSE,**kw)
            self.feed.after_close(requests,CLOSE)
        self.assertEqual(self.feed.opt['board'].retry,1)
        self.assertEqual(self.feed.opt['investors'].next,float('inf'))
        requests.posts.clear()
        with patch('ls_feed.time.monotonic',return_value=30):self.feed.after_close(requests,CLOSE)
        self.assertEqual([tr for tr,_ in requests.posts],['t2301'])
        self.assertEqual(self.feed.option_deriv.after_close_done,CLOSE.date())
        requests.posts.clear();self.feed.after_close(requests,CLOSE);self.assertEqual(requests.posts,[])


class GateTests(unittest.TestCase):
    def test_t1602_start_slots_are_shared_other_trs_independent(self):
        class ClockStop:
            def __init__(self):self.now=0;self.waited=[]
            def is_set(self):return False
            def wait(self,delay):self.waited.append(delay);self.now+=delay;return False
        stop=ClockStop();gate=RequestGate(stop,clock=lambda:stop.now)
        starts=[]
        for tr in ('t1602','t2301','t1602','t1602'):
            gate.acquire(tr);starts.append(stop.now)
        self.assertEqual(starts,[0,0,1.1,2.2]);self.assertEqual(stop.waited,[1.1,1.1])

    def test_stop_interrupts_waiting_request_without_network_start(self):
        stop=threading.Event();gate=RequestGate(stop,interval=60);gate.acquire('t1602')
        waiting=threading.Event();stopped=threading.Event()
        def worker():
            waiting.set()
            try:gate.acquire('t1602')
            except FeedStopped:stopped.set()
        thread=threading.Thread(target=worker);thread.start();self.assertTrue(waiting.wait(1))
        stop.set();thread.join(1)
        self.assertFalse(thread.is_alive());self.assertTrue(stopped.is_set())


if __name__=='__main__':unittest.main()
