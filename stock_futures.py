"""관심종목의 주식선물 근월물 시세·미결제·베이시스(LS t8401 마스터, t8402 현재가).

2026-11-04 장 마감 뒤 실조회:
  삼성전자 F 202611(A116B000) 219,000 · 현물 217,000 · 거래대금 7,393,586,680(천원 ≈ 7.4조) · 미결제 4,271,494(+1,904)
    시장 베이시스 +2,000 · 이론 베이시스 −833 · 기초자산 외국계 회원사 순매수 −1,867,581
  SK하이닉스 F 202611(A506B000) 1,403,000 · 현물 1,391,000 · 미결제 737,605(−9,181) · 시장 +12,000 · 이론 −5,929
종목별 '투자자' 수급은 LS·한투·키움 REST 어디에서도 찾지 못했다. 여기서는 시세·미결제·베이시스만 다룬다.
이론 베이시스는 배당 예상을 포함해 배당 시즌에 크게 흔들린다. 조회만 하며 키·토큰은 저장하지 않는다.
"""
import json
import re
from common import ROOT, atomic_json, in_session, now_kst, number, stamp

FILE = 'stock_futures.json'
STALE_SEC = 600  # 장중 기본 3분 주기의 3배 남짓
MONTH = re.compile(r'\sF\s+(\d{6})\s*$')
FIELDS = ('price', 'diff', 'volume', 'value', 'mgjv', 'mgjvdiff', 'sbasis', 'ibasis', 'theoryprice', 'baseprice',
          'basediff', 'basefornetbid', 'jandatecnt')


def front_months(master, codes, today):
    """t8401 행에서 종목코드별 근월물(이번 달 이후 가장 가까운 만기, 스프레드 제외)과 그다음 월물."""
    ym = today.strftime('%Y%m')
    out = {}
    for code in codes:
        rows = []
        for r in master or []:
            m = MONTH.search(str(r.get('hname', '')))
            if m and str(r.get('basecode', '')).strip().endswith(code) and m.group(1) >= ym:
                rows.append((m.group(1), str(r.get('shcode', '')).strip(), str(r.get('hname', '')).strip()))
        rows.sort()
        if rows:
            out[code] = rows[:2]
    return out


def parse(body, code, name, month, received):
    """t8402 응답 → 화면용 한 줄. 시장−이론 괴리는 선물이 이론가보다 비싼(+)·싼(−) 정도."""
    if str(body.get('rsp_cd', '')) != '00000':
        raise ValueError('LS t8402 응답 오류 %s %s' % (body.get('rsp_cd'), body.get('rsp_msg')))
    o = body.get('t8402OutBlock') or {}
    v = {k: number(o.get(k)) for k in FIELDS}
    if v['price'] is None or not v['price']:
        raise ValueError('LS t8402 %s 현재가가 없습니다' % code)
    gap = None if v['sbasis'] is None or v['ibasis'] is None else v['sbasis'] - v['ibasis']
    base = v['baseprice']
    return {'code': code, 'name': name, 'month': month, 'focode': str(o.get('focode') or ''), 'expiry': str(o.get('lastmonth') or ''),
            'days_left': v['jandatecnt'], 'price': v['price'], 'change_pct': v['diff'], 'base_price': base, 'base_change_pct': v['basediff'],
            'value_eok': None if v['value'] is None else round(v['value'] / 1e5),  # 천원 → 억원
            'volume': v['volume'], 'oi': v['mgjv'], 'oi_change': v['mgjvdiff'],
            'basis_market': v['sbasis'], 'basis_theory': v['ibasis'], 'basis_gap': gap,
            'basis_gap_pct': None if gap is None or not base else round(gap / base * 100, 2),
            'foreign_member_net': v['basefornetbid'], 'received_at': received.isoformat()}


def save(rows, received, errors, directory=None):
    atomic_json((directory or ROOT / 'data') / FILE, {'generated_at': received.isoformat(), 'rows': rows, 'errors': errors,
        'source': 'LS t8401·t8402 주식선물 근월물',
        'note': '종목별 투자자 수급은 제공되지 않아 시세·미결제·베이시스만 봅니다. 외국계 창구는 현물(기초자산) 회원사 순매수 수량이며 외국인 전체가 아닙니다. '
                '이론 베이시스는 배당 예상을 포함해 배당 시즌에 크게 흔들립니다.'})


def view(directory=None, cfg=None, at=None):
    try:
        data = json.loads(((directory or ROOT / 'data') / FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'status': 'empty', 'note': '장중 몇 분마다 LS t8402로 관심종목 주식선물 근월물을 조회하면 표시됩니다.'}
    at = stamp(at or now_kst())
    age = (at - stamp(data['generated_at'])).total_seconds()
    live = bool(cfg) and in_session(at, cfg)
    data['status'] = 'received'
    data['age_sec'] = round(age)
    data['stale'] = live and age > STALE_SEC
    data['mode'] = 'live' if live and not data['stale'] else 'stale' if data['stale'] else 'archive'
    return data
