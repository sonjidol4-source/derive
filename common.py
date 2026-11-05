"""시간·숫자·상품 식별: 네트워크 및 키움 모듈에 의존하지 않음."""
import datetime as dt
import json
import math
import os
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(__file__).resolve().parent

def now_kst():
    return dt.datetime.now(KST)

def stamp(value):
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=KST)
    return value.astimezone(KST)

def number(value, scale=1):
    if value is None or isinstance(value, bool):
        return None
    try:
        s = str(value).strip().replace(',', '').replace('%', '')
        if s.startswith('--'):
            s = '-' + s.lstrip('-')
        result = float(s) * float(scale)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None

def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    os.replace(str(tmp), str(path))

def config():
    return json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))

def business_day(day, cfg):
    return day.weekday() < 5 and day.isoformat() not in cfg['calendar']['holidays']

def last_trading_day(at, cfg):
    """조회 일자로 쓸 가장 최근 거래일. 휴장일·장 시작 전(09:05 이전)이면 직전 거래일, 아니면 오늘."""
    at = stamp(at)
    d = at.date()
    if business_day(d, cfg) and at.strftime('%H:%M') >= '09:05':
        return d
    for _ in range(14):
        d -= dt.timedelta(days=1)
        if business_day(d, cfg):
            return d
    raise ValueError('최근 거래일을 찾을 수 없습니다')

def monthly_expiry(month, cfg):
    y, m = int(month[:4]), int(month[4:6])
    first = dt.date(y, m, 1)
    expiry = first + dt.timedelta(days=(3 - first.weekday()) % 7 + 7)
    while not business_day(expiry, cfg):
        expiry -= dt.timedelta(days=1)
    return expiry

def nearest_month(months, at, cfg):
    at = stamp(at)
    eligible = []
    for month in sorted(set(months)):
        if len(month) != 6 or not month.isdigit():
            continue
        try:
            exp = monthly_expiry(month, cfg)
        except ValueError:
            continue
        if exp > at.date() or (exp == at.date() and at.strftime('%H:%M') < cfg['calendar']['expiry_cutoff']):
            eligible.append(month)
    return eligible[0] if eligible else None

def previous_futures_expiry(day, cfg):
    for back in range(16):
        ym = day.year * 12 + day.month - 1 - back
        y, m0 = divmod(ym, 12)
        m = m0 + 1
        if m not in (3, 6, 9, 12):
            continue
        expiry = monthly_expiry('%04d%02d' % (y, m), cfg)
        if expiry < day:
            return expiry
    raise ValueError('선물 만기 기준일을 찾을 수 없습니다')

def expected_days(start, end, cfg):
    out = []
    while start <= end:
        if business_day(start, cfg):
            out.append(start.isoformat())
        start += dt.timedelta(days=1)
    return out

def in_session(at, cfg, signals=False):
    at = stamp(at)
    start, end = cfg['session']['signal' if signals else 'collect']
    return business_day(at.date(), cfg) and start <= at.strftime('%H:%M') <= end

LABELS = {
    'k200': ('KOSPI200', 'p'), 'fut_price': ('근월 선물', 'p'), 'fut_prev_close': ('근월 선물 전일 종가', 'p'),
    'fut_oi': ('시장 전체 선물 미결제', '계약'), 'fut_oi_prev': ('미결제 전일 대비', '계약'),
    'basis': ('시장 베이시스', 'p'), 'basis_theory': ('이론 베이시스', 'p'),
    'basis_residual': ('시장−이론 베이시스', 'p'),
    'foreign_fut_qty': ('외국인 선물', '계약'), 'inst_fut_qty': ('기관계 선물', '계약'),
    'individual_fut_qty': ('개인 선물', '계약'), 'financial_fut_qty': ('금융투자 선물', '계약'),
    'foreign_fut_amt': ('외국인 선물 금액', '억원'), 'inst_fut_amt': ('기관계 선물 금액', '억원'),
    'individual_fut_amt': ('개인 선물 금액', '억원'), 'financial_fut_amt': ('금융투자 선물 금액', '억원'),
    'foreign_spot_amt': ('외국인 현물', '억원'), 'inst_spot_amt': ('기관계 현물', '억원'),
    'financial_spot_amt': ('금융투자 현물', '억원'), 'individual_spot_amt': ('개인 현물', '억원'),
    'program_amt': ('전체 프로그램', '억원'), 'nonarb_amt': ('비차익 프로그램', '억원'),
    'arb_amt': ('차익 프로그램', '억원'), 'breadth': ('상승 종목 비율', '%'),
    'foreign_call_amt': ('외국인 콜 순매수', '억원'), 'foreign_put_amt': ('외국인 풋 순매수', '억원'),
    'inst_call_amt': ('기관 콜 순매수', '억원'), 'inst_put_amt': ('기관 풋 순매수', '억원'),
}
