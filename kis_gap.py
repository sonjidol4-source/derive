"""'오늘 파생 수급' 차트(한투 1분 기록)의 공백을 LS t1617 시간대별 투자자 값으로 메운다. 화면 표시 전용 별도 보관.

한투 시장별 투자자매매동향은 '지금까지 당일 누적'만 주어 수집기가 꺼진 구간을 다시 받을 수 없다.
LS t1617(투자자매매종합2)은 선물·M선물(미니)·콜·풋·M콜·M풋을 30초 단위 시간대별로 준다(2026-11-05 확인).
같은 날 같은 시각 외국인 값 대조: 12:00:30 일반선물 −1,476 = −1,476, 미니 −2,991 = −2,991, 월물 콜 −35.34억 ≈ −35억.
- 선물 합계(일반 + 미니×0.2)는 한투 노란 선과 같은 기준으로 메운다.
- 옵션 콜−풋은 t1617에 위클리(월·목)가 없어 '월물+미니만'이다. 금액이 억 단위 정수라 소수점 아래가 없다.
- 연속키(tr_cont=Y, cts_time=HHMMSS00)를 주면 그 시각 이전 20행(10분)부터 받는다. 공백 구간만 거꾸로 받는다.
복구값은 실시간 판정·관측 기록에 쓰지 않는다.
"""
import datetime as dt
import json
from pathlib import Path
from common import KST, ROOT, atomic_json, now_kst, number

FILE = 'kis_gap_recovery.json'
# 이름, t1617 gubun1(시장), gubun2(1 수량·2 금액)
MARKETS = (('fut', '3', '1'), ('mini', '8', '1'), ('call', '4', '2'), ('put', '5', '2'), ('mini_call', '9', '2'), ('mini_put', '0', '2'))
INVESTORS = {'17': 'foreign', '18': 'inst', '08': 'individual', '01': 'financial'}
FIRST, LAST = '08:46', '15:45'  # 한투 장중 기록 구간(첫 분은 체결 전이라 제외)
MAX_PAGES = 45  # 한 공백당 최대(20행=10분 × 45 ≈ 7시간 반)


def _hm(t):
    return t.strftime('%H:%M')


def find_gaps(records, day, now, min_len=2):
    """한투 기록이 없는 분이 min_len분 이상 이어진 구간 [(시작, 끝)]. 진행 중인 마지막 3분은 빼고 본다."""
    have = {r['at'][11:16] for r in records if str(r.get('at', ''))[:10] == str(day) and (r.get('markets') or {}).get('fut')}
    t = dt.datetime.combine(day, dt.time.fromisoformat(FIRST), tzinfo=KST)
    end = dt.datetime.combine(day, dt.time.fromisoformat(LAST), tzinfo=KST)
    if now.date() == day:
        end = min(end, now - dt.timedelta(minutes=3))
    gaps, run = [], None
    while t <= end:
        if _hm(t) in have:
            if run and (run[1] - run[0]).seconds // 60 + 1 >= min_len:
                gaps.append((_hm(run[0]), _hm(run[1])))
            run = None
        else:
            run = (run[0], t) if run else (t, t)
        t += dt.timedelta(minutes=1)
    if run and (run[1] - run[0]).seconds // 60 + 1 >= min_len:
        gaps.append((_hm(run[0]), _hm(run[1])))
    return gaps


def fetch(call, market, start, end):
    """[start, end] 분 구간의 30초 행. call(path, tr, block, cont) → (body, tr_cont)."""
    _, g1, g2 = next(m for m in MARKETS if m[0] == market)
    after = (dt.datetime.combine(dt.date(2000, 1, 1), dt.time.fromisoformat(end)) + dt.timedelta(minutes=1)).strftime('%H%M') + '0000'
    stop = start.replace(':', '') + '0000'
    rows, cts = {}, after
    for _ in range(MAX_PAGES):
        body, cont = call('/stock/investor', 't1617', {'gubun1': g1, 'gubun2': g2, 'gubun3': '1', 'cts_date': '',
                                                       'cts_time': cts, 'exchgubun': 'K'}, 'Y')
        if str(body.get('rsp_cd')) != '00000':
            raise ValueError('LS t1617 오류 %s %s' % (body.get('rsp_cd'), body.get('rsp_msg')))
        page = body.get('t1617OutBlock1') or []
        for r in page:
            t = str(r.get('time', '')).strip()
            if len(t) >= 6 and t[:6].isdigit():
                rows[t[:6]] = {inv: number(r.get('sv_' + code)) for code, inv in INVESTORS.items()}
        nxt = str((body.get('t1617OutBlock') or {}).get('cts_time', '')).strip()
        if not page or not nxt or nxt >= cts or min(str(r.get('time', '')) for r in page) <= stop or cont != 'Y':
            break
        cts = nxt
    return rows


def load(directory, day):
    try:
        state = json.loads((Path(directory) / FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    if state.get('day') != str(day):
        state = {'day': str(day), 'rows': {m: {} for m, *_ in MARKETS}, 'covered': {m: [] for m, *_ in MARKETS}, 'jobs': []}
    return state


def covered(state, market, gap):
    return any(a <= gap[0] and gap[1] <= b for a, b in state['covered'].get(market, []))


def recover(call, directory, day, records, now=None):
    """아직 받지 않은 (공백, 시장)을 차례로 받는다. 호출 한도 등으로 중간에 멈추면 받은 것까지 저장하고 다음 회차에 이어 받는다."""
    now = now or now_kst()
    state = load(directory, day)
    gaps = find_gaps(records, day, now)
    todo = [(g, m) for g in gaps for m, *_ in MARKETS if not covered(state, m, g)]
    done, error = 0, None
    for gap, market in todo:
        try:
            rows = fetch(call, market, *gap)
        except Exception as e:  # 한도·네트워크: 받은 것까지 저장하고 다음 회차에
            error = str(e)[:200]
            break
        state['rows'][market].update(rows)
        state['covered'][market].append(list(gap))
        done += 1
        atomic_json(Path(directory) / FILE, state)
    if todo:
        state['jobs'] = (state.get('jobs') or [])[-9:] + [{'at': now.isoformat(), 'gaps': gaps, 'fetched': done, 'pending': len(todo) - done, 'error': error}]
        atomic_json(Path(directory) / FILE, state)
    return {'gaps': gaps, 'fetched': done, 'pending': len(todo) - done, 'error': error}


def _at(rows, minute):
    """그 분 30초 시점까지의 마지막 값(30초 행 중 HHMM30 이하)."""
    key = minute.replace(':', '') + '30'
    best = max((t for t in rows if t <= key and t >= minute.replace(':', '') + '00'), default=None)
    return rows.get(best) if best else None


def view(directory=None, day=None, records=None):
    """공백 분마다 외국인 선물 합계(일반+미니×0.2)와 옵션 콜−풋(월물+미니, 위클리 제외)."""
    directory = Path(directory or ROOT / 'data')
    day = day or now_kst().date()
    state = load(directory, day)
    have = {r['at'][11:16] for r in (records or []) if (r.get('markets') or {}).get('fut')}
    minutes = set()
    for m, *_ in MARKETS:
        for a, b in state['covered'].get(m, []):
            t = dt.datetime.combine(day, dt.time.fromisoformat(a))
            while _hm(t) <= b:
                minutes.add(_hm(t))
                t += dt.timedelta(minutes=1)
    points = []
    for minute in sorted(minutes - have):
        v = {m: _at(state['rows'].get(m, {}), minute) for m, *_ in MARKETS}
        def get(m, inv='foreign'):
            return (v[m] or {}).get(inv)
        fut, mini = get('fut'), get('mini')
        opts = [get(m) for m in ('call', 'put', 'mini_call', 'mini_put')]
        points.append({'at': '%sT%s:30+09:00' % (day, minute), 't': minute, 'foreign': {
            'fut': fut, 'mini': mini, 'fut_eq': None if fut is None or mini is None else round(fut + 0.2 * mini, 1),
            'opt_partial': None if any(x is None for x in opts) else opts[0] + opts[2] - opts[1] - opts[3]}})
    last = (state.get('jobs') or [{}])[-1]
    return {'day': str(day), 'points': points, 'job': last, 'source': 'LS t1617 시간대별(30초) 사후 조회',
            'note': '선물 합계는 한투 노란 선과 같은 기준(일반+미니×0.2)입니다. 옵션은 위클리(월·목)가 빠진 월물+미니 콜−풋이며 억 단위 정수입니다. '
                    '사후 조회 값이라 실시간 판정·관측 기록에는 쓰지 않습니다.'}
