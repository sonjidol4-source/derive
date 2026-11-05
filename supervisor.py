"""한 번만 실행되게 하고, 수집 흐름이 죽거나 멈추면 다시 띄운다.

2026-11-04 사고: 10:26·10:28 두 번 실행돼 수집기 둘이 같은 키로 조회했다. Windows는 HTTPServer의 SO_REUSEADDR 때문에
같은 포트(8767)를 두 프로세스가 함께 열 수 있어 두 번째 실행이 막히지 않았다. LS '호출 거래건수 초과'가 반복됐고
13:17 이후 LS 수집 흐름이 끝났는데, 수신 상태에는 마지막 '정상'이 그대로 남아 15:35까지 알아채지 못했다.
대책: ① 실행 잠금(이름 있는 뮤텍스)과 포트 독점 ② 수집 흐름의 예외를 회차 단위로 가두기(각 feed) ③ 이 감시기:
흐름이 죽었거나 heartbeat가 오래 멈추면 새로 띄우고, 장중에 갱신이 끊긴 수신 상태는 '응답 없음'으로 바꿔 보여 준다.
"""
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from common import ROOT, in_session, now_kst, stamp

_instance = None


def claim_instance(port):
    """같은 PC에서 이 레이더(같은 포트)가 이미 실행 중이면 False. 잡은 잠금은 프로세스가 끝날 때 풀린다."""
    global _instance
    if _instance is not None:
        return True
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k.CreateMutexW(None, False, 'Local\\deriv_radar_app_%d' % int(port))
        if not handle:
            return True  # 잠금을 만들 수 없으면 포트 독점에 맡긴다
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            k.CloseHandle(handle)
            return False
        _instance = handle
        return True
    import fcntl
    f = open(ROOT / ('.radar_%d.lock' % int(port)), 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _instance = f
    return True


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """다른 프로세스가 같은 포트를 열고 있으면 bind에서 실패한다(Windows 기본 동작과 달리)."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if sys.platform == 'win32' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ChildStop:
    """앱 종료(parent)와 이 흐름만의 교체(own) 둘 중 하나라도 켜지면 멈춘 것으로 본다. threading.Event와 같은 쓰임."""

    def __init__(self, parent):
        self.parent, self.own = parent, threading.Event()

    def is_set(self):
        return self.parent.is_set() or self.own.is_set()

    def set(self):
        self.own.set()

    def wait(self, timeout=None):
        end = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            left = 0.5 if end is None else min(0.5, end - time.monotonic())
            if left <= 0:
                break
            self.own.wait(left)
        return self.is_set()


# 흐름별 heartbeat 허용 시간(초). 한 회차 최악 소요(조회 수 × 응답 대기 한도)보다 넉넉하게.
BEAT_LIMITS = {'LS': 300, 'LS 옵션': 900, 'REST': 300, '한투': 600, 'LS 이력': 2400}
MAX_RESTARTS = 6  # 같은 흐름의 하루 자동 재시작 한도(무한 재시작으로 호출 한도를 소모하지 않게)


class Supervisor:
    """수집 흐름을 띄우고, 죽었거나(스레드 종료) 멈췄으면(heartbeat 초과) 새로 띄운다.

    멈춘 옛 흐름은 강제로 끝낼 수 없어 자기 stop만 켠다. LS는 응답이 돌아오면 check_stop에서 버리고,
    다른 흐름은 다음 반복 검사에서 끝난다. 재시작 이력은 data/feed_watchdog.log에 남긴다."""

    def __init__(self, engine, stop, clock=time.monotonic, log_dir=None):
        self.engine, self.stop, self.clock = engine, stop, clock
        self.log_dir = log_dir or engine.store.directory
        self.feeds = {}

    def add(self, name, factory, beats=None, on_start=None):
        """factory(stop) → 시작 전 스레드. beats(feed) → {이름: 마지막 heartbeat(monotonic)}."""
        self.feeds[name] = {'factory': factory, 'beats': beats or (lambda f: {name: getattr(f, 'beat', None)}),
                            'on_start': on_start, 'feed': None, 'stop': None, 'started': None, 'restarts': {}}
        return self.start(name)

    def start(self, name):
        s = self.feeds[name]
        s['stop'] = ChildStop(self.stop)
        s['feed'] = s['factory'](s['stop'])
        s['started'] = self.clock()
        s['feed'].start()
        if s['on_start']:
            s['on_start'](s['feed'])
        return s['feed']

    def problem(self, name):
        s = self.feeds[name]
        feed = s['feed']
        if not feed.is_alive():
            if getattr(feed, 'stopped_by_config', False):
                return None  # 키 없음·모의서버 등 의도한 종료
            return '수집 흐름이 끝남'
        now = self.clock()
        for label, beat in s['beats'](feed).items():
            last = beat if beat is not None else s['started']
            limit = BEAT_LIMITS.get(label, 600)
            if now - last > limit:
                return '%s %d초 동안 응답 없음(한도 %d초)' % (label, now - last, limit)
        return None

    def check(self):
        if self.stop.is_set():
            return
        for name, s in self.feeds.items():
            why = self.problem(name)
            if not why:
                continue
            day = now_kst().date().isoformat()
            count = s['restarts'].get(day, 0)
            if count >= MAX_RESTARTS:
                if not s.get('gave_up') == day:
                    s['gave_up'] = day
                    self.note(name, '수집 중단', '%s · 오늘 자동 재시작 %d회 한도에 도달했습니다. 실행창을 닫고 다시 실행하세요.' % (why, count))
                continue
            s['restarts'][day] = count + 1
            s['stop'].set()
            self.note(name, '자동 재시작', '%s → 새로 시작(오늘 %d회째)' % (why, count + 1))
            self.start(name)

    def note(self, name, status, detail):
        self.engine.connection('감시 · ' + name, status, detail)
        try:
            with open(self.log_dir / 'feed_watchdog.log', 'a', encoding='utf-8') as f:
                f.write('%s [%s] %s %s\n' % (now_kst().isoformat()[:19], name, status, detail))
        except OSError:
            pass


# 장중에 이 시간(초) 넘게 갱신이 없으면 수신 상태를 '응답 없음'으로 보여 준다. 조회 주기의 3~4배.
STALE_SEC = {'LS 선물 투자자': 120, 'LS 프로그램': 120, 'LS 투자자 종합': 120, 'LS 선물 시세': 120,
             'LS 옵션 전광판': 900, 'LS 옵션 투자자': 900, 'REST': 150, 'ka10051': 150, 'ka20001': 150,
             '관심종목': 420, '한투 파생 수급': 240}
QUIET = ('사용 안 함', '미설정', '라이브러리 미설치', '모의서버')


def mark_stale(connections, at, cfg):
    """장중인데 오래 갱신되지 않은 수신 상태를 '응답 없음'으로 바꾼 사본. 마지막 '정상'이 남아 멈춤을 가리지 않게."""
    at = stamp(at)
    if not in_session(at, cfg):
        return connections
    out = {}
    for name, c in connections.items():
        limit = STALE_SEC.get(name)
        try:
            age = (at - stamp(c.get('at'))).total_seconds()
        except (TypeError, ValueError):
            age = None
        if limit and age is not None and age > limit and c.get('status') not in QUIET:
            c = {**c, 'status': '응답 없음', 'stale': True,
                 'detail': '마지막 갱신 %s(%d분 전) · 직전 상태 %s · %s' % (str(c.get('at'))[11:19], age // 60, c.get('status'), c.get('detail', ''))}
        out[name] = c
    return out
