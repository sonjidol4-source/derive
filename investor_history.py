"""LS t1665(기간별투자자매매추이)로 선물·콜·풋 투자자별 '일별 확정' 순매수 이력을 받아 누적 포지션과 기준 분포를 만든다.

2026-10-23 실조회 확인(history_probe.py):
- market 4/upcode 900(선물), 5/800(풋), 6/700(콜), gubun2=1, gubun3=1(일별)로 2026-07-01~10-22 76거래일이 왔다.
- 10/22 값(선물 외국인 +1,552·기관계 −776·개인 −434·금융투자 +3,514계약, 금액 sa_17 3,137억)이 마감 확정치와 같았다.
  10/21 외국인 −3,632, 10/16 −4,200(장중 저장값과 다름).
- sv_* 계약, sa_* 억원. 투자자 코드: 17 외국인, 18 기관계, 08 개인, 01 금융투자. gubun2=2는 의미 미확인이라 쓰지 않는다.
- 선물 행은 응답 머리 mcode A016C000('코스피200 F 202612')로, 근월물 기준인지 선물 전체인지는 미확인.

누적은 '순매수의 합'이지 보유 포지션이 아니다(거래소는 투자자별 미결제를 주지 않는다). 롤오버는 따로 식별하지 않는다.
"""
import datetime as dt
import json
import threading
import time
from pathlib import Path

from common import ROOT, atomic_json, business_day, now_kst, previous_futures_expiry, stamp

FILE = 'investor_history.json'
MARKETS = [('fut', '4', '900'), ('put', '5', '800'), ('call', '6', '700')]
CODES = {'17': 'foreign', '18': 'inst', '08': 'individual', '01': 'financial'}
NAMES = {'foreign': '외국인', 'inst': '기관계', 'individual': '개인', 'financial': '금융투자'}
START = '20250101'


def parse(body):
    if str(body.get('rsp_cd', '')) != '00000':
        raise ValueError('LS t1665 응답 오류 %s %s' % (body.get('rsp_cd'), body.get('rsp_msg')))
    out = {}
    for r in body.get('t1665OutBlock1') or []:
        d = str(r.get('date', '')).strip()
        if len(d) != 8:
            continue
        day = '%s-%s-%s' % (d[:4], d[4:6], d[6:])
        qty, amt = {}, {}
        for code, key in CODES.items():
            q, a = r.get('sv_' + code), r.get('sa_' + code)
            try:
                if q is not None:
                    qty[key] = float(q)
                if a not in (None, ''):
                    amt[key] = float(a)
            except (TypeError, ValueError):
                continue
        out[day] = {'qty': qty, 'amt': amt}
    return out


def fetch(call, start=START, end=None, wait=lambda: time.sleep(1.2)):
    """call(path, tr, block) → body. 세 시장을 받아 날짜별로 합친다."""
    end = end or now_kst().strftime('%Y%m%d')
    days = {}
    for key, market, upcode in MARKETS:
        to = end
        for _ in range(12):  # 한 번에 오는 행 수가 제한되면 가장 이른 날짜 앞으로 이어 받는다
            body = call('/stock/chart', 't1665', {'market': market, 'upcode': upcode, 'gubun2': '1', 'gubun3': '1',
                                                  'from_date': start, 'to_date': to})
            wait()
            got = parse(body)
            new = {d: r for d, r in got.items() if (key not in days.get(d, {}))}
            for day, rec in new.items():
                days.setdefault(day, {})[key] = rec
            if not new:
                break
            earliest = min(got).replace('-', '')
            if earliest <= start:
                break
            to = (dt.date(int(earliest[:4]), int(earliest[4:6]), int(earliest[6:])) - dt.timedelta(days=1)).strftime('%Y%m%d')
    return days


def settled(days, at, cfg):
    """아직 끝나지 않은 날(오늘 16:20 전)의 행은 장중 값이라 '확정'으로 저장하지 않는다."""
    at = stamp(at)
    today = at.date().isoformat()
    open_today = business_day(at.date(), cfg) and at.strftime('%H:%M') < '16:20'
    return {d: r for d, r in days.items() if d < today or (d == today and not open_today)}


def load(directory):
    try:
        return json.loads((Path(directory) / FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'days': {}}


def save(directory, days, fetched_at):
    state = load(directory)
    state['days'].update(days)
    state['updated_at'] = fetched_at
    state['source'] = 'LS t1665 기간별투자자매매추이(일별 확정)'
    atomic_json(Path(directory) / FILE, state)
    return state


# ---------------------------------------------------------------- 계산

def series(state, market, inv):
    out = []
    for day in sorted(state.get('days') or {}):
        v = (((state['days'][day].get(market) or {}).get('qty')) or {}).get(inv)
        if v is not None:
            out.append((day, v))
    return out


def percentile(value, sample):
    """value가 표본 안에서 차지하는 위치(0~100, 작은 쪽부터)."""
    if value is None or not sample:
        return None
    below = sum(1 for x in sample if x < value)
    equal = sum(1 for x in sample if x == value)
    return round(100.0 * (below + 0.5 * equal) / len(sample), 1)


def cumulative(state, cfg, at):
    """투자자별 선물 순매수 누적: 직전 만기 다음 날부터 / 최근 20거래일, 과거 분포에서의 위치."""
    day = stamp(at).date()
    expiry = previous_futures_expiry(day, cfg)
    out = {}
    for inv in ('foreign', 'inst', 'individual'):
        s = series(state, 'fut', inv)
        if not s:
            continue
        since = [(d, v) for d, v in s if d > expiry.isoformat()]
        last20 = s[-20:]
        roll20 = [sum(v for _, v in s[i - 19:i + 1]) for i in range(19, len(s))]
        cur20 = sum(v for _, v in last20)
        out[inv] = {'label': NAMES[inv], 'since_expiry': sum(v for _, v in since), 'since_days': len(since),
                    'last20': cur20, 'last20_pct': percentile(cur20, roll20), 'last_day': s[-1][0], 'last': s[-1][1],
                    'path': [{'d': d, 'v': v} for d, v in since]}
    expected = [d for d in (expiry + dt.timedelta(days=i) for i in range(1, (day - expiry).days + 1))
                if business_day(d, cfg) and d < day]
    have = set(state.get('days') or {})
    missing = [d.isoformat() for d in expected if d.isoformat() not in have]
    return {'since': expiry.isoformat(), 'investors': out, 'missing': missing, 'days': len(have),
            'first_day': min(have) if have else None, 'updated_at': state.get('updated_at'),
            'note': '일별 확정 순매수의 합(LS t1665). 보유 포지션이 아니며 롤오버를 구분하지 않습니다. '
                    '20일 누적의 위치는 보관된 전체 기간의 20일 누적 분포 대비입니다.'}


def daily_rank(state, market, inv, value, n=60, before=None):
    """오늘 값(장중 누적)이 최근 n거래일 '마감 값' 분포에서 어디쯤인지. 장중에는 작게 나오는 점을 화면에 밝힌다.
    before(YYYY-MM-DD)를 주면 그 날짜 전 기록만 쓴다(과거 날짜를 다시 볼 때 이후 자료가 섞이지 않게)."""
    s = [v for d, v in series(state, market, inv) if not before or d < before][-n:]
    return {'pct': percentile(value, s), 'n': len(s), 'median_abs': sorted(abs(x) for x in s)[len(s) // 2] if s else None}


# ---------------------------------------------------------------- 수집기

class HistoryFeed(threading.Thread):
    """시작할 때 이력이 비었거나 낡았으면 받고, 평일 16:20 뒤·다음 날 08:30 뒤 한 번씩 다시 받아 확정치로 덮어쓴다."""

    def __init__(self, cfg, engine, stop, directory=None, call=None):
        super().__init__(daemon=True)
        self.cfg, self.engine, self.stop = cfg, engine, stop
        self.directory = Path(directory or (engine.store.directory if engine else ROOT / 'data'))
        self.call = call
        self.done = set()

    def status(self, state, detail):
        if self.engine:
            self.engine.connection('LS 투자자 일별 이력', state, detail)

    def slot(self, at):
        hm = at.strftime('%H:%M')
        if business_day(at.date(), self.cfg) and hm >= '16:20':
            return at.date().isoformat() + ' 마감 뒤'
        if hm >= '08:30':
            return at.date().isoformat() + ' 아침'
        return None

    def need(self, at):
        state = load(self.directory)
        if not state.get('days'):
            return 'first'
        s = self.slot(at)
        return s if s and s not in self.done else None

    def make_call(self):
        if self.call:
            return self.call
        import requests
        from ls_probe import BASE, credentials, token
        key, secret = credentials(self.cfg)
        access = token(key, secret)

        def call(path, tr, block):
            r = requests.post(BASE + path, timeout=20, json={tr + 'InBlock': block},
                              headers={'content-type': 'application/json; charset=utf-8', 'authorization': 'Bearer ' + access,
                                       'tr_cd': tr, 'tr_cont': 'N', 'tr_cont_key': ''})
            return r.json()
        return call

    def tick(self, at):
        why = self.need(at)
        if not why:
            return False
        state = load(self.directory)
        start = START if not state.get('days') else (stamp(at) - dt.timedelta(days=21)).strftime('%Y%m%d')
        days = fetch(self.make_call(), start=start, wait=lambda: self.stop.wait(1.2) if self.stop else time.sleep(1.2))
        days = settled(days, now_kst(), self.cfg)
        state = save(self.directory, days, now_kst().isoformat(timespec='seconds'))
        if self.engine and hasattr(self.engine, 'confirm_daily'):
            self.engine.confirm_daily(state)  # 일별 DB·CSV도 같은 확정치로 맞춘다(장중 잠정값은 *_intraday로 보존)
        self.done.add(why if why != 'first' else (self.slot(at) or 'first'))
        self.status('정상', '%d거래일 보관(%s~) · %s 갱신' % (len(state['days']), min(state['days']), why))
        return True

    def run(self):
        try:  # 시작할 때 이미 받아 둔 확정치로 일별 DB를 한 번 맞춘다
            state = load(self.directory)
            if state.get('days') and self.engine and hasattr(self.engine, 'confirm_daily'):
                self.engine.confirm_daily(state)
        except Exception as e:
            self.status('확인 필요', '일별 DB 확정치 반영 실패: %s' % str(e)[:120])
        while not self.stop.is_set():
            self.beat = time.monotonic()  # 감시기 heartbeat
            try:
                self.tick(now_kst())
            except Exception as e:  # 키·네트워크·응답 형식: 다음 회차에 다시
                self.status('수신 오류', str(e)[:160])
                self.stop.wait(600)
                continue
            self.stop.wait(120)
