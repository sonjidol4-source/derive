"""2026-11-04 사고 재발 방지: 중복 실행 차단, 수집 흐름 예외 격리, 감시기 재시작, '응답 없음' 표시."""
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from common import ROOT, config, stamp
import supervisor as S

CFG = config()


class Engine:
    def __init__(self, directory):
        self.connections = {}
        self.lock = threading.RLock()
        self.store = type('Store', (), {'directory': Path(directory)})()

    def connection(self, name, status, detail=''):
        self.connections[name] = {'status': status, 'detail': detail}


class Feed(threading.Thread):
    """run이 곧 끝나거나(die) 오래 붙잡혀 있는(hang) 가짜 수집 흐름."""
    def __init__(self, stop, behavior, log):
        super().__init__(daemon=True)
        self.stop, self.behavior, self.log = stop, behavior, log
        self.beat = time.monotonic()
        self.stopped_by_config = behavior == 'config'
        log.append(self)

    def run(self):
        if self.behavior in ('die', 'config'):
            return
        while not self.stop.is_set():
            if self.behavior == 'ok':
                self.beat = time.monotonic()
            self.stop.wait(0.05)


class ChildStopTests(unittest.TestCase):
    def test_parent_or_own_stops(self):
        parent = threading.Event()
        a, b = S.ChildStop(parent), S.ChildStop(parent)
        a.set()
        self.assertTrue(a.is_set()); self.assertFalse(b.is_set())
        parent.set()
        self.assertTrue(b.is_set())
        started = time.monotonic(); self.assertTrue(b.wait(5)); self.assertLess(time.monotonic() - started, 0.2)

    def test_wait_times_out(self):
        c = S.ChildStop(threading.Event())
        started = time.monotonic(); self.assertFalse(c.wait(0.3))
        self.assertGreaterEqual(time.monotonic() - started, 0.25)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.engine = Engine(self.tmp.name); self.stop = threading.Event(); self.addCleanup(self.stop.set)
        self.now = [1000.0]
        self.sup = S.Supervisor(self.engine, self.stop, clock=lambda: self.now[0])

    def test_dead_feed_is_restarted_and_logged(self):
        log = []
        self.sup.add('LS', lambda stop: Feed(stop, 'die' if not log else 'ok', log))
        log[0].join(1)
        self.sup.check()
        self.assertEqual(len(log), 2); self.assertTrue(log[1].is_alive())
        self.assertEqual(self.engine.connections['감시 · LS']['status'], '자동 재시작')
        self.assertIn('수집 흐름이 끝남', (Path(self.tmp.name) / 'feed_watchdog.log').read_text(encoding='utf-8'))

    def test_intended_stop_is_not_restarted(self):
        log = []
        self.sup.add('REST', lambda stop: Feed(stop, 'config', log))
        log[0].join(1); self.sup.check()
        self.assertEqual(len(log), 1)

    def test_hung_feed_is_replaced_and_old_one_told_to_stop(self):
        log = []
        self.sup.add('LS', lambda stop: Feed(stop, 'hang' if not log else 'ok', log),
                     beats=lambda f: {'LS': f.beat})
        log[0].beat = self.now[0]
        self.now[0] += S.BEAT_LIMITS['LS'] - 1; self.sup.check(); self.assertEqual(len(log), 1)
        self.now[0] += 5; self.sup.check()
        self.assertEqual(len(log), 2); self.assertTrue(log[0].stop.is_set()); self.assertFalse(log[1].stop.is_set())
        log[0].join(1); self.assertFalse(log[0].is_alive())
        self.assertIn('응답 없음', self.engine.connections['감시 · LS']['detail'])

    def test_restart_limit_per_day(self):
        log = []
        self.sup.add('LS', lambda stop: Feed(stop, 'die', log))
        for _ in range(S.MAX_RESTARTS + 3):
            for f in log: f.join(1)
            self.sup.check()
        self.assertEqual(len(log), 1 + S.MAX_RESTARTS)
        self.assertEqual(self.engine.connections['감시 · LS']['status'], '수집 중단')

    def test_on_start_receives_each_new_feed(self):
        log, seen = [], []
        self.sup.add('LS', lambda stop: Feed(stop, 'die' if not log else 'ok', log), on_start=seen.append)
        log[0].join(1); self.sup.check()
        self.assertEqual(seen, log)


class StaleTests(unittest.TestCase):
    def test_old_status_shown_as_no_response_in_session(self):
        conns = {'LS 선물 투자자': {'status': '정상', 'detail': 't1602', 'at': '2026-11-04T13:17:32+09:00'},
                 'REST': {'status': '정상', 'detail': '', 'at': '2026-11-04T15:35:20+09:00'},
                 '키움 OCX': {'status': '사용 안 함', 'detail': '', 'at': '2026-11-04T10:26:04+09:00'},
                 'LS 옵션 전광판': {'status': '정상', 'detail': '', 'at': '2026-11-04T15:30:00+09:00'}}
        out = S.mark_stale(conns, stamp('2026-11-04T15:35:46+09:00'), CFG)
        self.assertEqual(out['LS 선물 투자자']['status'], '응답 없음')
        self.assertIn('13:17:32(138분 전)', out['LS 선물 투자자']['detail'])
        self.assertEqual(out['REST']['status'], '정상')
        self.assertEqual(out['키움 OCX']['status'], '사용 안 함')
        self.assertEqual(out['LS 옵션 전광판']['status'], '정상')  # 5분 주기라 15분까지는 기다린다
        self.assertEqual(conns['LS 선물 투자자']['status'], '정상')  # 원본은 바꾸지 않는다

    def test_after_close_untouched(self):
        conns = {'LS 선물 투자자': {'status': '장외 대기', 'detail': '', 'at': '2026-11-04T13:00:00+09:00'}}
        self.assertEqual(S.mark_stale(conns, stamp('2026-11-04T17:00:00+09:00'), CFG), conns)


CLAIM = ("import sys,time;sys.path.insert(0,%r);import supervisor as S;"
         "print(S.claim_instance(%d),flush=True);time.sleep(%s)")


class SingleInstanceTests(unittest.TestCase):
    def test_second_process_cannot_claim(self):
        port = 59871
        first = subprocess.Popen([sys.executable, '-c', CLAIM % (str(ROOT), port, '5')], stdout=subprocess.PIPE, text=True)
        self.addCleanup(first.kill); self.addCleanup(first.stdout.close)
        self.assertEqual(first.stdout.readline().strip(), 'True')
        second = subprocess.run([sys.executable, '-c', CLAIM % (str(ROOT), port, '0')], capture_output=True, text=True, timeout=30)
        self.assertEqual(second.stdout.strip(), 'False')
        first.kill(); first.wait(10)
        third = subprocess.run([sys.executable, '-c', CLAIM % (str(ROOT), port, '0')], capture_output=True, text=True, timeout=30)
        self.assertEqual(third.stdout.strip(), 'True')  # 앞 프로세스가 끝나면 다시 실행할 수 있다

    def test_port_is_exclusive(self):
        from http.server import BaseHTTPRequestHandler
        a = S.ExclusiveHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler); self.addCleanup(a.server_close)
        port = a.server_address[1]
        with self.assertRaises(OSError):
            S.ExclusiveHTTPServer(('127.0.0.1', port), BaseHTTPRequestHandler)


class FeedLoopTests(unittest.TestCase):
    """예상 못 한 예외(DB 잠김 등)가 나도 수집 흐름이 끝나지 않고 다음 회차로 간다."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.engine = Engine(self.tmp.name)

    def test_ls_fast_loop_survives_unexpected_error(self):
        from ls_feed import LsFeed
        stop = threading.Event(); feed = LsFeed(CFG, self.engine, stop)
        calls = []
        def once(*args):
            calls.append(1)
            if len(calls) == 1: raise sqlite3.OperationalError('database is locked')
            stop.set()
        feed.fast_once = once
        feed.stop.wait = lambda t=None: stop.is_set()
        feed.fast_loop(None, 'k', 's')
        self.assertEqual(len(calls), 2)
        self.assertIn('database is locked', self.engine.connections['LS 선물 투자자']['detail'])
        self.assertIn('선물 흐름 예외', (Path(self.tmp.name) / 'ls_fast.log').read_text(encoding='utf-8'))

    def test_rest_loop_survives_unexpected_error(self):
        import rest_feed
        stop = threading.Event(); feed = rest_feed.RestFeed(CFG, self.engine, stop)
        feed.client = type('C', (), {'env': 'live'})()
        calls = []
        def once(cfg):
            calls.append(1)
            if len(calls) == 1: raise ConnectionError('reset')
            stop.set()
        feed.once = once
        orig = rest_feed.Client
        rest_feed.Client = lambda env: feed.client
        self.addCleanup(setattr, rest_feed, 'Client', orig)
        orig_wait = stop.wait; stop.wait = lambda t=None: stop.is_set()
        feed.run()
        self.assertEqual(len(calls), 2)
        self.assertIn('reset', self.engine.connections['REST']['detail'])


if __name__ == '__main__':
    unittest.main()
