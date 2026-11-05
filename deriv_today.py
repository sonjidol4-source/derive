"""오늘 파생 포지션 한눈에: 투자자별 선물·옵션 순매수(한투)와 행사가별 미결제 변화(LS 옵션 전광판).

근거(2026-10-22 실조회):
- 한투 시장별 투자자매매동향(시세) FHPTJ04030000. 공식 명세의 시장코드로 선물(K2I/F001)·콜(OC01)·풋(OP01)·
  미니(MKI/F004·OC02·OP02)·위클리 월(WKM/OC05·OP05)·목(WKI/OC04·OP04)을 따로 받는다.
- 10/22 마감 뒤 값: 선물 4개 투자자 수량이 LS t1602와 같았다. 콜(OC01)은 LS 시장구분 6, 풋(OP01)은 5와 8개 값이 모두 같았다.
- 금액 원문은 백만원(선물 외국인 313,722 = LS 3,137억). 화면은 억원.
- 미니선물·미니옵션은 승수 5만 원(일반 25만 원)이라 일반선물 계약 수로 0.2배 환산한다.
- 한투 옵션 전광판은 장외에 먼 행사가 100개만 주어(다음 쪽 없음) 쓰지 않는다. 행사가별 미결제는 LS t2301 전 행사가 조회를 쓴다.

해석 기준: 같은 시각 과거 분포(한투 기록 20거래일 이상) → 없으면 LS 일별 확정 이력 중앙값을 장 경과 비율로 줄인 값 → 둘 다 없으면 고정 초기값(FUT_FLAT·OPT_FLAT).
2026-10-23 외부 검토 반영: 외국인 선물은 동행 성격이 강하고(Ryu·Webb·Yu 2024) 예측 근거가 있는 것은 외국인 옵션 신규 매수(Ryu & Yang 2018)라,
투자자 순매수와 미결제 증감을 엮은 '신규/청산 추정'을 따로 낸다(행사가별 투자자 자료가 없어 상품 단위 추정). 금액은 옵션 프리미엄 순매수라 포지션 크기나 손익이 아니다.

2026-10-23 2차 검토(GPT) 반영:
- 필수 시장(선물·미니선물, 콜·풋 각 4개)이 하나라도 빠지거나 조회 시각이 30초 넘게 벌어지면 합계·방향 문장을 내지 않는다(부분합은 따로 표시).
- 콜−풋은 '프리미엄 순매수대금 차이'일 뿐 델타(상승·하락 노출)가 아니므로 '상승 쪽/하락 쪽' 대신 '콜 대금 우위/풋 대금 우위'로 쓰고,
  콜·풋 동반 순매수·순매도에 전략 의미(변동 대비·박스권)를 붙이지 않는다.
- 시장 전체 미결제는 모든 투자자의 거래가 섞여 외국인의 신규·청산을 식별할 수 없다(같은 관측을 만드는 반례가 있음).
  그래서 '신규/청산 추정'을 없애고 '외국인 매수 우위 · 미결제 증가 동반 · 신규/청산 구분 불가' 같은 관측으로만 쓴다.
- 미결제가 늘어난 행사가는 '지지·저항'이라 단정하지 않는다. 콜은 지수 위, 풋은 지수 아래만 고르고 '미결제 증가'로 표기한다.
- 마감 값은 필수 시장이 모두 온 기록이 있어야 완료로 본다(18:00까지 1분 간격 재시도).
"""
import datetime as dt
import json
import threading
import time
from pathlib import Path

from common import ROOT, atomic_json, business_day, now_kst, number, stamp

INVESTOR_PATH = '/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market'
INVESTOR_TR = 'FHPTJ04030000'
# key, 이름, 시장, 업종, 종류, 일반 계약 환산 배수
MARKETS = [
    ('fut', '선물', 'K2I', 'F001', 'fut', 1.0),
    ('mini', '미니선물', 'MKI', 'F004', 'fut', 0.2),
    ('call', '콜(월물)', 'K2I', 'OC01', 'call', 1.0),
    ('put', '풋(월물)', 'K2I', 'OP01', 'put', 1.0),
    ('wk_mon_call', '위클리(월) 콜', 'WKM', 'OC05', 'call', 1.0),
    ('wk_mon_put', '위클리(월) 풋', 'WKM', 'OP05', 'put', 1.0),
    ('wk_thu_call', '위클리(목) 콜', 'WKI', 'OC04', 'call', 1.0),
    ('wk_thu_put', '위클리(목) 풋', 'WKI', 'OP04', 'put', 1.0),
    ('mini_call', '미니 콜', 'MKI', 'OC02', 'call', 0.2),
    ('mini_put', '미니 풋', 'MKI', 'OP02', 'put', 0.2),
]
INVESTORS = [('foreign', 'frgn', '외국인'), ('inst', 'orgn', '기관계'), ('individual', 'prsn', '개인'), ('financial', 'scrt', '금융투자')]
FUT_FLAT = 300   # 일반선물 환산 계약. 이보다 작으면 '뚜렷하지 않음'
OPT_FLAT = 20    # 억원(콜 순매수 − 풋 순매수). 이보다 작으면 '방향 약함'
POLL_SEC = 60
START, END = '08:45', '15:50'  # 선물·옵션 정규장 08:45~15:45. 15:50은 체결이 아니라 마지막 조회 시각
FINAL_AFTER = '16:15'  # LS 선물 투자자 값이 16:07경까지 바뀐 사례가 있어 그 뒤에 마지막 값을 받는다
FINAL_UNTIL = '18:00'  # 일부 시장이 실패하면 이 시각까지 다시 받는다
MAX_SPAN_SEC = 30      # 한 기록 안 시장별 조회 시각이 이보다 벌어지면 같은 시각 합계로 보지 않는다
FLOW_DIR = 'kis_flows'
WEEKLY_FILE = 'weekly_board.json'
WEEKLY_SEC = 300
SESSION_OPEN, SESSION_MIN = (8, 45), 420  # 선물 08:45~15:45
OI_FILE = 'option_oi.json'


# ---------------------------------------------------------------- 수집

def parse_row(row):
    out = {}
    for key, code, _ in INVESTORS:
        q = number(row.get(code + '_ntby_qty'))
        a = number(row.get(code + '_ntby_tr_pbmn'))
        out[key] = {'qty': q, 'amt': None if a is None else round(a / 100.0, 2)}  # 백만원 → 억원
    return out


def poll(rest, clock=now_kst):
    """10개 시장을 차례로 조회. 실패한 시장은 errors에 남기고 값은 비운다(0으로 채우지 않음)."""
    markets, errors = {}, []
    started = clock()
    for key, label, iscd, iscd2, _, _ in MARKETS:
        try:
            body = rest.get(INVESTOR_PATH, INVESTOR_TR, {'FID_INPUT_ISCD': iscd, 'FID_INPUT_ISCD_2': iscd2})
            out = body.get('output')
            row = (out[0] if out else {}) if isinstance(out, list) else (out or {})
            parsed = parse_row(row)
            if all(v['qty'] is None and v['amt'] is None for v in parsed.values()):
                # 성공 코드여도 값이 비어 있으면 '0'이 아니라 받지 못한 것이다(빈 시장을 완료로 치지 않게).
                errors.append('%s: 응답은 정상이나 값이 비어 있음' % label)
                continue
            markets[key] = parsed
        except Exception as e:  # KisError·네트워크 오류: 다른 시장은 계속 받는다
            errors.append('%s: %s' % (label, str(e)[:120]))
    at = clock()
    return {'at': at.isoformat(timespec='seconds'), 'started': started.isoformat(timespec='seconds'),
            'day': at.date().isoformat(), 'source': 'KIS ' + INVESTOR_TR, 'markets': markets, 'errors': errors}


def span_sec(record):
    try:
        return (stamp(record['at']) - stamp(record['started'])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return 0.0  # 'started'가 없던 이전 기록은 한 번에 받은 것으로 본다


def complete(record):
    """필수 10개 시장의 값이 모두 있고 조회 시각이 MAX_SPAN_SEC 안인가."""
    m = record.get('markets') or {}
    def filled(k):  # 시장 이름만 있고 값이 전부 비면 받지 못한 것으로 본다(실제 0은 숫자 0이라 통과)
        return any(v.get('qty') is not None for v in (m.get(k) or {}).values())
    return all(filled(k) for k, *_ in MARKETS) and span_sec(record) <= MAX_SPAN_SEC


def save(record, directory):
    d = Path(directory) / FLOW_DIR
    d.mkdir(parents=True, exist_ok=True)
    with open(d / (record['day'] + '.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def load_day(directory, day):
    p = Path(directory) / FLOW_DIR / (day + '.jsonl')
    rows = []
    try:
        for line in p.read_text(encoding='utf-8').splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def latest_day(directory):
    d = Path(directory) / FLOW_DIR
    try:
        days = sorted(p.stem for p in d.glob('*.jsonl'))
    except OSError:
        return None
    return days[-1] if days else None


# ---------------------------------------------------------------- 요약·해석

def _sum(parts):
    vals = [v for v in parts if v is not None]
    return (sum(vals) if vals else None), len(vals) < len(parts)


def summarize(record):
    """투자자별 선물(미니 환산 포함)·콜·풋 합계."""
    m = record.get('markets') or {}
    names = {k: label for k, label, *_ in MARKETS}
    late = span_sec(record) > MAX_SPAN_SEC
    out = {}
    for inv, _, label in INVESTORS:
        def get(key, field):
            return ((m.get(key) or {}).get(inv) or {}).get(field)
        fut, mini = get('fut', 'qty'), get('mini', 'qty')
        fut_missing = [names[k] for k in ('fut', 'mini') if get(k, 'qty') is None]
        # 미니선물이 빠졌을 때 0으로 더하면 '합계'가 일반선물 값으로 바뀌어 보이므로 합계를 비운다.
        fut_eq = None if fut_missing or late else fut + 0.2 * mini
        side, opt_missing = {}, []
        for kind in ('call', 'put'):
            keys = [k for k, _, _, _, kd, _ in MARKETS if kd == kind]
            amt, partial = _sum([get(k, 'amt') for k in keys])
            opt_missing += [names[k] for k in keys if get(k, 'amt') is None]
            side[kind] = {'total': None if partial or late else amt, 'partial_sum': amt if partial else None, 'partial': partial,
                          'parts': {k: get(k, 'amt') for k in keys}, 'qty': {k: get(k, 'qty') for k in keys}}
        bias = None if side['call']['total'] is None or side['put']['total'] is None else side['call']['total'] - side['put']['total']
        out[inv] = {'label': label, 'fut': fut, 'mini': mini, 'fut_eq': fut_eq, 'fut_amt': get('fut', 'amt'),
                    'call': side['call'], 'put': side['put'], 'opt_bias': bias,
                    'mini_missing': mini is None, 'fut_missing': fut_missing, 'opt_missing': opt_missing,
                    'span_late': late}
    return out


def direction(value, flat):
    if value is None:
        return None
    return 1 if value >= flat else -1 if value <= -flat else 0


def elapsed_fraction(at):
    at = stamp(at)
    start = at.replace(hour=SESSION_OPEN[0], minute=SESSION_OPEN[1], second=0, microsecond=0)
    return min(1.0, max(0.15, (at - start).total_seconds() / 60.0 / SESSION_MIN))


def same_time_values(directory, inv, field, at, days=60, exclude=None):
    """지난 거래일들의 같은 시각(±5분) 기록 값. 한투 1분 기록이 쌓일수록 정확해진다."""
    d = Path(directory) / FLOW_DIR
    try:
        names = sorted(p.stem for p in d.glob('*.jsonl'))
    except OSError:
        return []
    hm = stamp(at).strftime('%H:%M')
    out = []
    # 과거 날짜를 나중에 열 때 그 날짜 이후 기록이 기준에 섞이지 않도록 기준일(exclude, 보통 그 기록의 날짜)보다 앞선 날만 쓴다.
    names = [n for n in names if not exclude or n < exclude]
    for name in names[-days:]:
        best = None
        for rec in load_day(directory, name):
            t = rec['at'][11:16]
            if abs(_mins(t) - _mins(hm)) <= 5 and (best is None or abs(_mins(t) - _mins(hm)) < abs(_mins(best['at'][11:16]) - _mins(hm))):
                best = rec
        if best:
            v = summarize(best)[inv][field]
            if v is not None:
                out.append(v)
    return out


def _mins(hm):
    return int(hm[:2]) * 60 + int(hm[3:5])


def thresholds(directory, hist, inv, at, today=None, live=True):
    """방향 판단 기준. 같은 시각 분포(20일 이상) > LS 일별 확정 이력 > 고정 초기값."""
    out = {}
    for field, fixed, unit in (('fut_eq', FUT_FLAT, '계약'), ('opt_bias', OPT_FLAT, '억')):
        same = same_time_values(directory, inv, field, at, exclude=today) if live else []
        if len(same) >= 20:
            med = sorted(abs(x) for x in same)[len(same) // 2]
            out[field] = {'flat': max(med, 1.0), 'basis': '같은 시각 최근 %d거래일 |값| 중앙값 %s%s' % (len(same), format(round(med), ','), unit),
                          'sample': same}
            continue
        if field == 'fut_eq' and hist and (hist.get('days') or {}):
            import investor_history as H
            r = H.daily_rank(hist, 'fut', inv, 0, before=today)
            if r['n'] >= 20 and r['median_abs']:
                frac = elapsed_fraction(at) if live else 1.0
                flat = max(100.0, 0.5 * r['median_abs'] * frac)
                # LS 일별 이력은 일반선물만이라 '일반+미니×0.2' 합계와 기준이 정확히 같지 않다(근사). 한투 기록 20일이 쌓이면 합계끼리 비교로 바뀐다.
                out[field] = {'flat': flat, 'basis': '일반선물 최근 %d거래일 마감 |순매수| 중앙값 %s계약의 절반 × 장 경과 %.0f%% (미니 미포함 이력이라 합계에는 근사)' % (r['n'], format(round(r['median_abs']), ','), frac * 100)}
                continue
        out[field] = {'flat': fixed, 'basis': '고정 초기값 ±%s%s(검증 전)' % (format(fixed, ','), unit)}
    return out


def option_word(s, flat):
    """콜−풋 순매수대금의 방향을 '어느 다리가 만들었는지'로 부른다. 프리미엄 차이라 델타·상승하락 노출이 아니다.
    예: 콜 −90·풋 +14 → '콜 순매도 주도', 콜 −439·풋 −267 → '콜·풋 모두 순매도(콜이 더 큼)'."""
    c, p = s['call']['total'], s['put']['total']
    od = direction(s['opt_bias'], flat)
    cd_, pd_ = direction(c, flat), direction(p, flat)
    if cd_ == pd_ and cd_ in (1, -1):
        return '콜·풋 모두 %s(%s이 더 큼)' % ('순매수' if cd_ > 0 else '순매도', '콜' if abs(c) >= abs(p) else '풋'), None
    if not od:
        return '콜·풋 대금 차이 작음', None
    call_lead = abs(c) >= abs(p)
    word = ('콜 순매수' if c > 0 else '콜 순매도') if call_lead else ('풋 순매수' if p > 0 else '풋 순매도')
    share = (abs(c) if call_lead else abs(p)) / ((abs(c) + abs(p)) or 1)
    return word + ' 주도', round(share * 100)


def reading(s, th=None):
    """한 투자자의 한 줄 요약. 관측(숫자)만 말하고, 빠진 시장이 있으면 판정을 보류한다.

    옵션 콜−풋은 프리미엄 순매수대금 차이라 델타(상승·하락 노출)와 부호가 다를 수 있다. 그래서 '콜 대금 우위/풋 대금 우위'로만 쓴다.
    side(+1/−1)는 '선물 매수 우위·콜 대금 우위가 지수 상승에 앞선다'는 가설을 관측 기록으로 검증하기 위한 값이지 예측이 아니다."""
    th = th or {'fut_eq': {'flat': FUT_FLAT}, 'opt_bias': {'flat': OPT_FLAT}}
    FF, OF = th['fut_eq']['flat'], th['opt_bias']['flat']
    fd, od = direction(s['fut_eq'], FF), direction(s['opt_bias'], OF)
    late = s.get('span_late')
    if s['fut_eq'] is None:
        if late:
            fut_text = '선물 판정 보류(시장별 조회 시각 차이 %d초 초과)' % MAX_SPAN_SEC
        elif s.get('fut_missing') and s['fut'] is not None:
            fut_text = '선물 판정 보류(%s 값 없음 · 일반선물만 %s계약)' % (', '.join(s['fut_missing']), '{:+,}'.format(round(s['fut'])))
        else:
            fut_text = '선물 값 없음'
    else:
        word = {1: '매수 우위', -1: '매도 우위', 0: '뚜렷하지 않음'}[fd]
        fut_text = '선물 %s %s계약' % (word, '{:+,}'.format(round(s['fut_eq'])))
        if s['mini']:
            fut_text += '(일반 %s · 미니 %s)' % ('{:+,}'.format(round(s['fut'])), '{:+,}'.format(round(s['mini'])))
    if s['opt_bias'] is None:
        if late:
            opt_text = '옵션 판정 보류(시장별 조회 시각 차이 %d초 초과)' % MAX_SPAN_SEC
        elif s.get('opt_missing'):
            opt_text = '옵션 판정 보류(%s 값 없음 · 부분합은 전체 합계가 아님)' % ', '.join(s['opt_missing'])
        else:
            opt_text = '옵션 값 없음'
    else:
        word, share = option_word(s, OF)
        opt_text = '옵션 %s: 콜−풋 순매수대금 %s억(콜 %s · 풋 %s)%s' % (word, '{:+,.0f}'.format(s['opt_bias']),
                                                           '{:+,.0f}'.format(s['call']['total']), '{:+,.0f}'.format(s['put']['total']),
                                                           ' · 차이의 %d%%' % share if share is not None else '')
    fw = {1: '선물 매수 우위', -1: '선물 매도 우위'}
    ow, opt_flat = {}, '옵션 대금 차이 작음'
    if s['opt_bias'] is not None:
        w = option_word(s, OF)[0]
        if w.startswith('콜·풋 모두'):
            # 콜·풋을 함께 사거나 판 경우 차이의 부호만으로 방향 쪽을 정하지 않는다.
            od, opt_flat = 0, w + ' → 옵션 방향 판단 안 함'
        elif od in (1, -1):
            ow = {od: w}
    if fd is None or od is None:
        headline, side = ('자료 불완전 → 판단 보류' if (fd is None and od is None) or late else
                          '%s · 옵션 자료 불완전' % fw.get(fd, '선물 뚜렷하지 않음') if od is None else
                          '선물 자료 불완전 · %s' % ow.get(od, '옵션 대금 차이 작음')), 0
    elif fd in (1, -1) and od in (1, -1):
        headline = ('%s · %s (같은 쪽)' % (fw[fd], ow[od])) if fd == od else '%s · %s → 엇갈림, 판단 보류' % (fw[fd], ow[od])
        side = fd if fd == od else 0
    elif fd in (1, -1):
        headline, side = '%s · %s' % (fw[fd], opt_flat), fd
    elif od in (1, -1):
        headline, side = '%s · 선물 뚜렷하지 않음' % ow[od], od
    else:
        headline, side = ('뚜렷한 쏠림 없음' if opt_flat == '옵션 대금 차이 작음' else '선물 뚜렷하지 않음 · ' + opt_flat), 0
    return {'fut_text': fut_text, 'opt_text': opt_text, 'headline': headline, 'side': side}


def momentum(total, ch, flat):
    """당일 선물 순매수(합계)의 방향과 최근 5·15분 변화를 비교해 '강화/약화/전환'을 적는다. 관측일 뿐 예측이 아니다.
    작은 흔들림을 걸러 내려고 15분 변화가 판단 기준의 절반(5분은 1/4) 이상일 때만 말한다."""
    if total is None:
        return None
    m5, m15 = (ch.get('m5') or {}).get('fut_eq'), (ch.get('m15') or {}).get('fut_eq')
    pick = [(m, v, lim) for m, v, lim in ((15, m15, flat * 0.5), (5, m5, flat * 0.25)) if v is not None and abs(v) >= lim]
    if not pick:
        return {'text': '최근 15분 큰 변화 없음', 'state': 0} if m15 is not None else None
    m, v, _ = pick[0]
    side = 1 if total > 0 else -1 if total < 0 else 0
    word = '매수' if side > 0 else '매도'
    if side == 0:
        state, text = (1 if v > 0 else -1), '최근 %d분 %s계약 → %s 쪽으로 움직이는 중' % (m, '{:+,}'.format(round(v)), '매수' if v > 0 else '매도')
    elif (v > 0) == (side > 0):
        state, text = 2, '당일 %s 우위가 최근 %d분 %s계약 더 커짐(강화)' % (word, m, '{:+,}'.format(round(v)))
    elif abs(v) >= abs(total):
        state, text = -2, '최근 %d분 %s계약으로 당일 %s 우위를 넘어서는 반대 흐름(전환 관찰)' % (m, '{:+,}'.format(round(v)), word)
    else:
        state, text = -1, '당일 %s 우위가 최근 %d분 %s계약 줄어듦(약화)' % (word, m, '{:+,}'.format(round(v)))
    return {'text': text + ' · 관측이며 신규·청산은 구분 불가', 'state': state, 'minutes': m, 'change': v}


def change(series, inv, field, minutes, now):
    """minutes 전(±3분 안에서 가장 가까운) 기록 대비 변화."""
    target = now - dt.timedelta(minutes=minutes)
    best = None
    for rec in series:
        t = stamp(rec['at'])
        if abs((t - target).total_seconds()) <= 180 and (best is None or abs((t - target).total_seconds()) < abs((stamp(best['at']) - target).total_seconds())):
            best = rec
    if best is None:
        return None
    old = summarize(best)[inv][field]
    cur = summarize(series[-1])[inv][field]
    return None if old is None or cur is None else cur - old


# ---------------------------------------------------------------- 행사가별 미결제 변화(LS 옵션 전광판 조회본)

def track_oi(directory):
    """LS가 저장한 snapshot_calls/puts.json이 새로 바뀌면 날짜별 처음·마지막 미결제를 option_oi.json에 남긴다."""
    directory = Path(directory)
    try:
        calls = json.loads((directory / 'snapshot_calls.json').read_text(encoding='utf-8'))['snapshot']
        puts = json.loads((directory / 'snapshot_puts.json').read_text(encoding='utf-8'))['snapshot']
    except (OSError, ValueError, KeyError):
        return None
    if calls.get('month') != puts.get('month'):
        return None
    at = max(calls['received_at'], puts['received_at'])
    day = at[:10]
    try:
        spot = number(json.loads((directory / 'snapshot_futures.json').read_text(encoding='utf-8'))['snapshot']['values'].get('k200'))
    except (OSError, ValueError, KeyError):
        spot = None
    snap = {'at': at, 'month': calls['month'], 'metrics': option_metrics(calls['rows'], puts['rows'], spot),
            'oi': {('%g%s' % (r['strike'], r['cp'])): r.get('oi') for s in (calls, puts) for r in s['rows'] if r.get('oi') is not None}}
    path = directory / OI_FILE
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {'days': {}}
    rec = state['days'].setdefault(day, {})
    if (rec.get('last') or {}).get('at') == at:
        if (rec['last'].get('metrics') or {}).get('method') == METRIC_METHOD:
            return state
        rec['last']['metrics'] = snap['metrics']  # 지표 추가 전·계산 방식 변경 전 저장본에 한 번 붙인다
        if rec.get('first', {}).get('at') == at:
            rec['first']['metrics'] = snap['metrics']
        if rec.get('series') and rec['series'][-1].get('at') == at:
            rec['series'][-1] = {'at': at, **snap['metrics']}
        atomic_json(path, state)
        return state
    rec.setdefault('first', snap)
    rec['last'] = snap
    rec.setdefault('series', []).append({'at': at, **snap['metrics']})
    for old in sorted(state['days'])[:-6]:
        del state['days'][old]
    atomic_json(path, state)
    return state


# 2026-10-29 검토: 행사가 하나의 IV를 그대로 쓰면 장중에 ATM IV 37~53%, 스큐 −11~+19로 몇 분 사이 뒤집혔다.
# LS IV는 마지막 체결가로 계산돼, 거래가 드문 2.5 간격 행사가(미결제 수십 계약)는 오래된 가격 탓에 35~69%, 0.01까지 나온다.
# 같은 시각 5 간격 주요 행사가는 48~51%로 고르다. 그래서 미결제가 일정 이상인 행사가만, 기준 행사가 주변 여러 개의 중앙값을 쓴다.
METRIC_METHOD = 2
IV_MIN_OI = 100      # 이보다 미결제가 적은 행사가의 IV는 쓰지 않는다
IV_RANGE = (1, 200)  # LS가 0.01 같은 값을 줄 때가 있다
IV_PICK = 4          # 기준 행사가에 가까운 순으로 이만큼(콜·풋 각각)의 중앙값


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _liquid_iv(rows, cp, target, pick=IV_PICK):
    """target에 가까운 유동성 있는 행사가 pick개의 IV 중앙값과 쓴 행사가."""
    xs = [r for r in rows if r.get('cp') == cp and r.get('strike') is not None and (r.get('oi') or 0) >= IV_MIN_OI
          and r.get('iv') is not None and IV_RANGE[0] < r['iv'] < IV_RANGE[1]]
    xs = sorted(xs, key=lambda r: abs(r['strike'] - target))[:pick]
    return (_median([r['iv'] for r in xs]), sorted(r['strike'] for r in xs)) if xs else (None, [])


def option_metrics(calls, puts, spot):
    """ATM IV, 스큐(지수 −5% 풋 IV − +5% 콜 IV), 미결제 풋콜비율, 콜·풋 미결제 합계. IV는 LS 응답(%)의 유동 행사가 중앙값."""
    rows = list(calls) + list(puts)
    call_oi = sum(r.get('oi') or 0 for r in calls)
    put_oi = sum(r.get('oi') or 0 for r in puts)
    out = {'call_oi': call_oi, 'put_oi': put_oi, 'pcr_oi': round(put_oi / call_oi, 3) if call_oi else None,
           'spot': spot, 'atm_iv': None, 'skew': None, 'method': METRIC_METHOD}
    if spot:
        (ac, ak), (ap, pk) = _liquid_iv(rows, 'C', spot), _liquid_iv(rows, 'P', spot)
        ivs = [v for v in (ac, ap) if v is not None]
        out['atm_iv'] = round(sum(ivs) / len(ivs), 2) if ivs else None
        out['atm_strikes'] = sorted(set(ak + pk))
        (lo, lk), (hi, hk) = _liquid_iv(rows, 'P', spot * 0.95), _liquid_iv(rows, 'C', spot * 1.05)
        if lo is not None and hi is not None:
            out['skew'] = round(lo - hi, 2)
            out['skew_strikes'] = [lk, hk]
    return out


def vol_view(directory):
    """월물 옵션의 ATM IV·스큐·풋콜비율: 오늘 마지막 값과 전 거래일 마지막 값, 오늘 장중 흐름."""
    try:
        state = json.loads((Path(directory) / OI_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    days = [d for d in sorted(state.get('days') or {}) if (state['days'][d].get('last') or {}).get('metrics')]
    if not days:
        return None
    cur = state['days'][days[-1]]['last']['metrics']
    prev = state['days'][days[-2]]['last']['metrics'] if len(days) > 1 else None
    method = cur.get('method')
    def diff(k):
        if not prev or cur.get(k) is None or prev.get(k) is None:
            return None
        if k in ('atm_iv', 'skew') and prev.get('method') != method:
            return None  # 계산 방식이 다른 날끼리는 IV·스큐를 비교하지 않는다
        return round(cur[k] - prev[k], 3)
    out = {'day': days[-1], 'asof': state['days'][days[-1]]['last']['at'], 'cur': cur, 'prev_day': days[-2] if prev else None,
           'chg': {k: diff(k) for k in ('atm_iv', 'skew', 'pcr_oi', 'call_oi', 'put_oi')},
           'series': [{'t': x['at'][11:16], 'atm_iv': x.get('atm_iv') if x.get('method') == method else None,
                       'skew': x.get('skew') if x.get('method') == method else None, 'pcr_oi': x.get('pcr_oi')}
                      for x in state['days'][days[-1]].get('series', [])][-120:]}
    parts = []
    c = out['chg']
    # 전일 값이 없어도 오늘 값은 보여 주고, 전일 대비는 있을 때만 붙인다.
    if cur.get('atm_iv') is not None:
        parts.append('ATM IV %.1f%%' % cur['atm_iv'] + ('(전일 대비 %+.1f)' % c['atm_iv'] if c['atm_iv'] is not None else ''))
    if cur.get('skew') is not None:
        parts.append('스큐 %+.1f' % cur['skew'] + ('(전일 대비 %+.1f → %s)' % (c['skew'], '풋 IV 상대 상승' if c['skew'] > 0.5 else '풋 IV 상대 하락' if c['skew'] < -0.5 else '변화 작음')
                                               if c['skew'] is not None else ''))
    if cur.get('pcr_oi') is not None:
        parts.append('미결제 풋/콜 %.2f' % cur['pcr_oi'])
    if prev and prev.get('method') != method and (cur.get('atm_iv') is not None or cur.get('skew') is not None):
        parts.append('IV·스큐 계산 방식을 바꿔(유동 행사가 중앙값) 전일 대비는 다음 거래일부터 표시')
    missing = [name for k, name in (('atm_iv', 'ATM IV'), ('skew', '스큐')) if cur.get(k) is None]
    if missing:
        parts.append('%s 없음(지수 값 또는 행사가 IV 미수신)' % '·'.join(missing))
    out['headline'] = ' · '.join(parts) if parts else '옵션 전광판 조회가 쌓이면 표시됩니다'
    out['note'] = ('월물 옵션(LS 전광판) 기준. IV는 미결제 %d계약 이상 행사가만, 기준 행사가 주변 %d개(콜·풋 각각)의 중앙값입니다. '
                   'IV·스큐는 변동성(위험) 국면 판단용이며 방향 신호가 아닙니다(Chun·Cho·Ryu 2019; Han 외 2012).' % (IV_MIN_OI, IV_PICK))
    return out


# ---------------------------------------------------------------- 위클리 옵션 전광판(한투)

def weekly_poll(rest, cache):
    """WKM(월요일 만기)·WKI(목요일 만기) 중 가장 가까운 만기의 콜·풋 전광판. 월물리스트는 하루 한 번만 조회한다.

    2026-10-23 확인: 위클리 FID_MTRT_CNT는 월물리스트의 mtrt_yymm(YYMMWW, 예 261004)이고 4자리 코드는 0행."""
    board = '/uapi/domestic-futureoption/v1/quotations/'
    out = {}
    today = now_kst().date().isoformat()
    for cls, label in (('WKM', '위클리(월)'), ('WKI', '위클리(목)')):
        key = cls + ':' + today
        if key not in cache:
            body = rest.get(board + 'display-board-option-list', 'FHPIO056104C0',
                            {'FID_COND_SCR_DIV_CODE': '509', 'FID_COND_MRKT_DIV_CODE': '', 'FID_COND_MRKT_CLS_CODE': cls})
            cache[key] = [str(r.get('mtrt_yymm') or '').strip() for r in (body.get('output') or []) if r.get('mtrt_yymm')]
        for code in cache[key]:
            b = rest.get(board + 'display-board-callput', 'FHPIF05030100',
                         {'FID_COND_MRKT_DIV_CODE': 'O', 'FID_COND_SCR_DIV_CODE': '20503', 'FID_MRKT_CLS_CODE': 'CO',
                          'FID_MTRT_CNT': code, 'FID_MRKT_CLS_CODE1': 'PO', 'FID_COND_MRKT_CLS_CODE': cls})
            rows = []
            for cp, blk in (('C', 'output1'), ('P', 'output2')):
                for r in b.get(blk) or []:
                    k = number(r.get('acpr'))
                    if k is None:
                        continue
                    rows.append({'strike': k, 'cp': cp, 'oi': number(r.get('hts_otst_stpl_qty')) or 0,
                                 'oi_chg': number(r.get('otst_stpl_qty_icdc')) or 0, 'iv': number(r.get('hts_ints_vltl')),
                                 'vol': number(r.get('acml_vol'))})
            if sum(r['oi'] for r in rows) > 0:  # 만기가 끝난(미결제 0) 주차는 건너뛰고 다음 주차
                out[cls] = {'label': label, 'code': code, 'rows': rows}
                break
    return out


def weekly_view(directory, spot, band=0.07, top=3):
    try:
        state = json.loads((Path(directory) / WEEKLY_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    out = {'asof': state.get('at'), 'boards': []}
    for cls, b in (state.get('boards') or {}).items():
        rows = [r for r in b['rows'] if not spot or abs(r['strike'] / spot - 1) <= band]
        tot = {cp: sum(r['oi_chg'] for r in b['rows'] if r['cp'] == cp) for cp in 'CP'}
        def pick(cp):
            side_ok = (lambda r: True) if not spot else (lambda r: r['strike'] > spot) if cp == 'C' else (lambda r: r['strike'] < spot)
            return sorted([r for r in rows if r['cp'] == cp and r['oi_chg'] > 0 and side_ok(r)], key=lambda r: -r['oi_chg'])[:top]
        out['boards'].append({'cls': cls, 'label': b['label'], 'code': b['code'], 'call_oi_chg': tot['C'], 'put_oi_chg': tot['P'],
                              'call_oi': sum(r['oi'] for r in b['rows'] if r['cp'] == 'C'),
                              'put_oi': sum(r['oi'] for r in b['rows'] if r['cp'] == 'P'),
                              'call_up': pick('C'), 'put_up': pick('P')})
    return out


# ---------------------------------------------------------------- 외국인 옵션 매매와 미결제 증감(관측만)

def flow_with_oi(net, oi_chg, side):
    """투자자 순매수(계약)와 그 상품 전체 미결제 증감을 나란히 적는다.

    시장 전체 미결제에는 모든 투자자의 거래가 섞여 외국인의 신규·청산을 식별할 수 없다(외국인 숏환매와 다른 투자자끼리의 신규가
    같은 '+순매수·+미결제'를 만든다). 그래서 신규/청산 판정은 하지 않는다."""
    if net is None or oi_chg is None or abs(net) < 50:
        return None
    return '%s 외국인 %s · 상품 전체 미결제 %s 동반 (신규/청산 구분 불가)' % (
        side, '매수 우위' if net > 0 else '매도 우위', '증가' if oi_chg > 0 else '감소' if oi_chg < 0 else '변화 없음')


def estimates(record, directory, inv='foreign'):
    """외국인 옵션 순매수와 상품 미결제 증감. 월물 미결제는 LS 전광판(전 거래일 마지막 대비), 위클리는 한투 전광판 증감."""
    m = (record or {}).get('markets') or {}
    def q(key):
        return ((m.get(key) or {}).get(inv) or {}).get('qty')
    out = []
    vol = vol_view(directory)
    if vol and vol.get('prev_day'):
        for side, key, oik in (('월물 콜', 'call', 'call_oi'), ('월물 풋', 'put', 'put_oi')):
            r = flow_with_oi(q(key), vol['chg'].get(oik), side)
            if r:
                out.append({'text': '%s: 외국인 %s계약 · 미결제 %s(%s 대비) → %s' % (side, '{:+,}'.format(round(q(key))), '{:+,}'.format(round(vol['chg'][oik])),
                                                                         vol['prev_day'][5:].replace('-', '/'), r), 'side': 0})
    wk = weekly_view(directory, None)
    for b in (wk or {}).get('boards', []):
        k = 'wk_mon' if b['cls'] == 'WKM' else 'wk_thu'
        for side, cp, oik in (('콜', 'call', 'call_oi_chg'), ('풋', 'put', 'put_oi_chg')):
            r = flow_with_oi(q('%s_%s' % (k, cp)), b[oik], '%s %s' % (b['label'], side))
            if r:
                out.append({'text': '%s %s: 외국인 %s계약 · 미결제 %s(전일 대비) → %s' % (b['label'], side, '{:+,}'.format(round(q('%s_%s' % (k, cp)))),
                                                                              '{:+,}'.format(round(b[oik])), r), 'side': 0})
    return out


def oi_view(directory, spot, band=0.07, top=4):
    try:
        state = json.loads((Path(directory) / OI_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'status': 'unavailable', 'note': 'LS 옵션 전광판 조회(장중 5분마다)가 쌓이면 표시됩니다.'}
    days = sorted(state.get('days') or {})
    if not days:
        return {'status': 'unavailable', 'note': '행사가별 미결제 기록이 아직 없습니다.'}
    today = state['days'][days[-1]]
    cur = today['last']
    base, base_label = None, ''
    for d in reversed(days[:-1]):
        prev = state['days'][d].get('last')
        if prev and prev.get('month') == cur['month']:
            base, base_label = prev, '%s 마지막 조회(%s) 대비' % (d[5:].replace('-', '/'), prev['at'][11:16])
            break
    if base is None:
        base, base_label = today['first'], '오늘 첫 조회(%s) 대비' % today['first']['at'][11:16]
    rows = []
    for key, oi in cur['oi'].items():
        strike, cp = float(key[:-1]), key[-1]
        if spot and abs(strike / spot - 1) > band:
            continue
        before = base['oi'].get(key)
        if before is None:
            continue
        pos = None if not spot else ('위' if strike > spot else '아래' if strike < spot else '같음')
        rows.append({'strike': strike, 'cp': cp, 'oi': oi, 'chg': oi - before, 'pos': pos})
    def pick(cp, sign):
        # 콜은 지수 위(외가), 풋은 지수 아래(외가)만. 지수 값이 없으면 위·아래를 모르므로 고르지 않는다.
        want = '위' if cp == 'C' else '아래'
        xs = [r for r in rows if r['cp'] == cp and r['chg'] * sign > 0 and r['pos'] == want]
        return sorted(xs, key=lambda r: -abs(r['chg']))[:top]
    out = {'status': 'ok', 'month': cur['month'], 'asof': cur['at'], 'base': base_label, 'spot': spot,
           'band_pct': band * 100, 'call_up': pick('C', 1), 'put_up': pick('P', 1),
           'call_down': pick('C', -1), 'put_down': pick('P', -1), 'same_snapshot': base['at'] == cur['at']}
    cu, pu = out['call_up'][:1], out['put_up'][:1]
    parts = []
    if cu:
        parts.append('지수 위 콜 미결제 증가 최대 %g(%s)' % (cu[0]['strike'], '{:+,}'.format(round(cu[0]['chg']))))
    if pu:
        parts.append('지수 아래 풋 미결제 증가 최대 %g(%s)' % (pu[0]['strike'], '{:+,}'.format(round(pu[0]['chg']))))
    if not spot:
        out['headline'] = '지수 값이 없어 행사가의 위·아래를 구분하지 못했습니다'
    else:
        out['headline'] = ' · '.join(parts) if parts else ('아직 비교할 변화가 없습니다' if out['same_snapshot'] else '지수 근처 외가에서 늘어난 미결제가 없습니다')
    out['note'] = ('미결제는 매수·매도 양쪽이 함께 만드는 값이라 누가 팔았는지, 딜러가 감마 매수인지 매도인지 알 수 없습니다. '
                   '증가가 지지·저항으로 작동하는지는 검증 전 가설입니다.')
    return out


# ---------------------------------------------------------------- 화면용

def view(directory, cfg, at=None, spot=None):
    directory = Path(directory or ROOT / 'data')
    at = stamp(at or now_kst())
    day = latest_day(directory)
    if spot is None:
        try:
            snap = json.loads((directory / 'snapshot_futures.json').read_text(encoding='utf-8'))['snapshot']
            spot = number(snap['values'].get('k200'))
        except (OSError, ValueError, KeyError):
            spot = None
    oi = oi_view(directory, spot)
    if not day:
        return {'status': 'unavailable', 'oi': oi, 'vol': vol_view(directory),
                'note': '한투 키를 등록하고 실시간실행을 켜 두면 장중 1분마다 투자자별 선물·옵션 순매수를 받아 표시합니다.'}
    series = [r for r in load_day(directory, day) if r.get('markets')]
    if not series:
        return {'status': 'unavailable', 'oi': oi, 'note': '오늘 받은 한투 수급 기록이 없습니다.'}
    last = series[-1]
    last_at = stamp(last['at'])
    s = summarize(last)
    import investor_history as H
    hist = H.load(directory)
    live_day = day == at.date().isoformat()
    investors = []
    for inv, _, label in INVESTORS:
        th = thresholds(directory, hist, inv, last_at, today=day, live=last['at'][11:16] < FINAL_AFTER)
        item = {'key': inv, **s[inv], 'reading': reading(s[inv], th),
                'basis': {k: v['basis'] for k, v in th.items()},
                'flat': {k: round(v['flat'], 2) for k, v in th.items()}}  # 화면 동조 판정도 같은 기준을 쓴다
        if inv != 'financial' and s[inv]['fut'] is not None:
            item['rank'] = H.daily_rank(hist, 'fut', inv, s[inv]['fut'], before=day)
        item['change'] = {f'm{m}': {'fut_eq': change(series, inv, 'fut_eq', m, last_at),
                                    'opt_bias': change(series, inv, 'opt_bias', m, last_at)} for m in (5, 15, 60)}
        item['momentum'] = momentum(s[inv]['fut_eq'], item['change'], th['fut_eq']['flat'])
        investors.append(item)
    points = []
    tail = series[-420:]
    for i, rec in enumerate(tail):
        su = summarize(rec)
        point = {'t': rec['at'][11:16], 'at': rec['at'], **{inv: {'fut_eq': su[inv]['fut_eq'], 'opt_bias': su[inv]['opt_bias']} for inv, _, _ in INVESTORS}}
        if i >= len(tail) - 20:
            # 화면의 '외국인 선물·옵션 동조 분석' 5·15분 비교용: 최근 20회차만 일반·미니선물과 상품별 콜·풋 상세를 붙인다.
            f = su['foreign']
            point['foreign'].update({'fut': f['fut'], 'mini': f['mini'],
                                     'call': {k: f['call'][k] for k in ('total', 'partial', 'parts')},
                                     'put': {k: f['put'][k] for k in ('total', 'partial', 'parts')}})
            point['complete'] = complete(rec) and not su['foreign']['span_late']
        points.append(point)
    age = (at - last_at).total_seconds()
    live = live_day and age <= POLL_SEC * 3 + 60
    cum = H.cumulative(hist, cfg, at) if hist.get('days') else None
    return {'status': 'live' if live else 'archive', 'day': day, 'asof': last['at'], 'age_sec': age,
            'errors': last.get('errors') or [], 'investors': investors, 'series': points, 'oi': oi,
            'cumulative': cum, 'vol': vol_view(directory), 'weekly': weekly_view(directory, spot),
            'estimates': estimates(last, directory),
            'complete': complete(last), 'span_sec': span_sec(last),
            'caution': '모두 관측된 순매수(거래 흐름)이며 보유 포지션·상승확률이 아닙니다. 외국인 선물 순매수는 지수와 같이 움직이는 동행 성격이 강하다는 연구가 있고(Ryu·Webb·Yu 2024), '
                       '외국인 옵션 신규 매수의 다음 날 예측력 연구(Ryu & Yang 2018)는 이 화면의 콜−풋 대금·장중 신호를 검증한 것이 아닙니다. '
                       '판정 문구는 관측 기록으로 검증 중인 가설입니다.',
            'flat': {'fut': FUT_FLAT, 'opt': OPT_FLAT}, 'source': '한투 시장별 투자자매매동향(1분 주기) · 행사가 미결제는 LS 옵션 전광판'}


# ---------------------------------------------------------------- 수집기

class DerivTodayFeed(threading.Thread):
    """장중(08:45~15:50) 1분마다 한투 투자자 수급을 받고, 16:15 뒤 그날 마지막 값을 한 번 더 받는다.

    키가 없거나 한투 접속이 안 돼도 LS 행사가 미결제 기록(track_oi)은 계속 한다."""

    def __init__(self, cfg, engine, stop, directory=None):
        super().__init__(daemon=True)
        self.cfg, self.engine, self.stop = cfg, engine, stop
        self.directory = Path(directory or engine.store.directory)
        self.rest = None
        self.final_done = None
        self.next_poll = 0.0
        self.next_weekly = 0.0
        self.weekly_cache = {}
        self.marks = {}
        self.beat = time.monotonic()  # 감시기 heartbeat

    def status(self, state, detail):
        if self.engine:
            self.engine.connection('한투 파생 수급', state, detail)

    def connect(self):
        import requests
        import kis_api as kis
        path = kis.credentials_path(self.cfg)
        key, secret = kis.load_credentials(path)
        self.rest = kis.Rest(requests, key, secret, path.parent / 'kis_token.json', min_gap=0.4)

    def due(self, at):
        hm = at.strftime('%H:%M')
        if not business_day(at.date(), self.cfg):
            return None
        if START <= hm <= END:
            return 'session'
        if FINAL_AFTER <= hm <= FINAL_UNTIL and self.final_done != at.date() and not any(
                r['at'][11:16] >= FINAL_AFTER and complete(r) for r in load_day(self.directory, at.date().isoformat())):
            return 'final'
        return None

    def tick(self, at):
        try:
            track_oi(self.directory)
        except (OSError, ValueError, KeyError):
            pass
        kind = self.due(at)
        if kind is None:
            self.status('장외 대기', '장중 08:45~15:50 1분마다 조회, 16:15 뒤 마감 값을 한 번 더 받습니다')
            return
        if time.monotonic() < self.next_poll:
            return
        if self.rest is None:
            self.connect()
        record = poll(self.rest)
        if record['markets']:
            save(record, self.directory)
            if kind == 'session':
                # 신호 시각은 조회를 시작한 때가 아니라 자료를 다 받은 때(record['at'])로 남긴다.
                self.observe(record, stamp(record['at']))
        self.next_poll = time.monotonic() + POLL_SEC
        if kind == 'final' or time.monotonic() >= self.next_weekly:
            try:
                boards = weekly_poll(self.rest, self.weekly_cache)
                atomic_json(self.directory / WEEKLY_FILE, {'at': now_kst().isoformat(timespec='seconds'), 'boards': boards})
            except Exception as e:  # 위클리 실패는 투자자 수급과 따로 표시
                if self.engine:
                    self.engine.connection('한투 위클리 옵션', '수신 오류', str(e)[:160])
            self.next_weekly = time.monotonic() + WEEKLY_SEC
        if kind == 'final' and complete(record):  # 일부 시장이 빠진 마감 조회는 완료로 치지 않고 1분 뒤 다시 받는다
            self.final_done = at.date()
        ok = len(record['markets'])
        self.status('정상' if not record['errors'] else '일부 오류',
                    '시장 %d/%d · %s%s' % (ok, len(MARKETS), record['at'][11:19],
                                           (' · ' + record['errors'][0]) if record['errors'] else ''))

    def observe(self, record, at):
        """투자자 판정이 방향을 바꾸면 기존 관측 기록에 넣어 5·15·30·60분 뒤 지수 변화로 검증한다(같은 방향 반복은 기록 안 함)."""
        if not self.engine:
            return
        import investor_history as H
        hist = H.load(self.directory)
        s = summarize(record)
        for inv in ('foreign', 'inst'):
            th = thresholds(self.directory, hist, inv, at, today=record['day'])
            r = reading(s[inv], th)
            if r['side'] in (1, -1) and self.marks.get(inv) != r['side']:
                kind = '파생 수급 %s %s(가설 검증)' % (s[inv]['label'], '매수·콜 우위' if r['side'] > 0 else '매도·풋 우위')
                if self.engine.observe(kind, r['side'], at, [r['headline'], r['fut_text'], r['opt_text']],
                                        {'source': 'deriv_today', 'investor': inv, 'basis': {k: v['basis'] for k, v in th.items()},
                                         'thresholds': th, 'rule_context':{'method':'deriv-today-1'},
                                         'input_observations': [{'source':record.get('source','KIS'),
                                             'at':record['at'], 'quality':'동일 회차 완전 조회' if complete(record) else '부분 조회',
                                             'provenance':'observed'}]}):
                    self.marks[inv] = r['side']
            elif r['side'] == 0 and complete(record):  # 자료가 빠진 회차는 판정 없음이라 이전 판정을 지우지 않는다
                self.marks.pop(inv, None)

    def run(self):
        while not self.stop.is_set():
            self.beat = time.monotonic()
            try:
                self.tick(now_kst())
            except Exception as e:  # 키 없음·네트워크·응답 형식: 다른 수집기는 계속 돈다
                name = type(e).__name__
                self.status('미설정' if name == 'MissingKeys' else '수신 오류', str(e)[:160])
                self.rest = None if name != 'MissingKeys' else self.rest
                self.stop.wait(60 if name != 'MissingKeys' else 300)
                continue
            self.stop.wait(5)
