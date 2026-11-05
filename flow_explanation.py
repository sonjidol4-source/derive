"""Read-only presentation of the existing verdict; never creates trading events."""
import datetime as dt
import math
from common import stamp


NAMES = {'foreign_fut_qty': '외국인 선물', 'foreign_spot_amt': '외국인 현물',
         'nonarb_amt': '비차익 프로그램', 'program_amt': '전체 프로그램', 'k200': 'KOSPI200'}


def flow_speed(engine, key, at):
    """Two disjoint windows, one shared boundary, actual source elapsed times."""
    at = stamp(at)
    result = {'key': key, 'name': NAMES[key], 'available': False,
              'reason': '15분의 연속 관측이 필요합니다.'}
    history = [h for h in engine.history if stamp(h['at']).date() == at.date() and stamp(h['at']) <= at]
    points = []
    for minutes in (15, 5):
        target = at - dt.timedelta(minutes=minutes)
        # Select by the source timestamp, not the minute when a delayed reply was cached.
        # All replies here were already known by the current decision time.
        candidates = [h for h in history if h['values'].get(key,{}).get('at')
                      and stamp(h['values'][key]['at']) <= target]
        if not candidates:
            return result
        h = max(candidates,key=lambda h:stamp(h['values'][key]['at']))
        row = h['values'].get(key)
        if not row or not row.get('at') or (target-stamp(row['at'])).total_seconds() > min(engine.flow_ttl(row), engine.cfg['quality']['max_gap_sec']):
            return result
        points.append((stamp(h['at']), row))
    current = engine.measurement(key, at)
    if not current['fresh']:
        return {**result, 'reason': '현재 자료가 없거나 수신이 지연됐습니다.'}
    points.append((at, current))
    identity = lambda row: (row.get('source'), row.get('scope'), row.get('quality'))
    expected = identity(current)
    chain = [(stamp(h['at']), h['values'].get(key)) for h in history if stamp(h['at']) >= points[0][0]]
    chain.append((at, current))
    valid_chain = []
    for observed, row in chain:
        if not row:
            continue
        if engine.manual(row):
            return {**result, 'reason': '수동 입력 구간입니다.'}
        if identity(row) != expected:
            return {**result, 'reason': '출처·집계 범위·자료 종류가 바뀐 구간입니다.'}
        age = (observed-stamp(row['at'])).total_seconds()
        if not 0 <= age <= engine.flow_ttl(row):
            continue
        valid_chain.append((observed, row))
    chain = valid_chain
    times = [t for t, _ in chain]
    if any((b-a).total_seconds() > engine.cfg['quality']['max_gap_sec'] for a, b in zip(times, times[1:])):
        return {**result, 'reason': '관측 공백이 포함된 구간입니다.'}
    source_times = [stamp(row['at']) for _, row in chain]
    if any(b < a or (b-a).total_seconds() > engine.cfg['quality']['max_gap_sec'] for a, b in zip(source_times, source_times[1:])):
        return {**result, 'reason': '원자료 시각이 연속적이지 않습니다.'}
    for index, ((boundary, row), target) in enumerate(zip(points, (at-dt.timedelta(minutes=15), at-dt.timedelta(minutes=5), at))):
        tolerance = engine.flow_ttl(row) if index == 2 else min(engine.flow_ttl(row), engine.cfg['quality']['max_gap_sec'])
        if not 0 <= (target-stamp(row['at'])).total_seconds() <= tolerance:
            return {**result, 'reason': '비교 기준 시각에 가까운 원자료가 부족합니다.'}
    a, b, c = [row for _, row in points]
    ta, tb, tc = [stamp(row['at']) for row in (a, b, c)]
    prior_minutes, recent_minutes = (tb-ta).total_seconds()/60, (tc-tb).total_seconds()/60
    if min(prior_minutes, recent_minutes) <= 0:
        return {**result, 'reason': '비교할 원자료의 시간 간격이 부족합니다.'}
    prior, recent = b['value']-a['value'], c['value']-b['value']
    old_rate, new_rate = prior/prior_minutes, recent/recent_minutes
    same_rate = math.isclose(new_rate, old_rate, rel_tol=1e-9, abs_tol=1e-9)
    if recent < 0:
        text = ('순매도 지속 · 순매도 속도 '+('동일' if same_rate else '완화' if new_rate > old_rate else '확대')) if prior < 0 else '순매수·보합 → 순매도'
    elif recent > 0:
        text = ('순매수 지속 · 순매수 속도 '+('동일' if same_rate else '확대' if new_rate > old_rate else '완화')) if prior > 0 else '순매도·보합 → 순매수'
    else:
        text = '최근 구간 순매매 변화 없음'
    return {**result, 'available': True, 'reason': '', 'text': text,
            'prior': prior, 'recent': recent, 'prior_minutes': prior_minutes, 'recent_minutes': recent_minutes,
            'prior_rate': old_rate, 'recent_rate': new_rate,
            'start_at': ta.isoformat(), 'boundary_at': tb.isoformat(), 'end_at': tc.isoformat(),
            'source': current.get('source', ''), 'scope': current.get('scope', '')}


def build_explanation(engine, at, metrics, price5, cash_key, directions, label, guide, price_oi):
    rules = engine.cfg['rules']
    keys = ('foreign_fut_qty', cash_key, 'k200')
    limits = (rules['foreign_fut_5m'], rules['foreign_spot_5m'] if cash_key == 'foreign_spot_amt' else rules['program_5m'], rules['price_5m_pct'])
    values = (metrics['foreign_fut_qty']['m5'], metrics[cash_key]['m5'], price5)
    axes = []
    for key, value, limit, direction in zip(keys, values, limits, directions):
        unit = '%' if key == 'k200' else '계약' if key.endswith('_qty') else '억원'
        state = ('자료 부족 · 수신 시각·구간 관측 확인' if direction is None else
                 ('상승' if key == 'k200' else '매수')+' 기준 충족' if direction == 1 else
                 ('하락' if key == 'k200' else '매도')+' 기준 충족' if direction == -1 else '기준 미달')
        axes.append({'key': key, 'name': NAMES[key], 'value': value, 'unit': unit,
                     'threshold': limit, 'direction': direction, 'state': state,
                     'window':getattr(engine,'window_details',{}).get(key,{}).get('m5',{}),
                     'asof': engine.values.get(key, {}).get('at'),
                     'source': engine.values.get(key, {}).get('source', '')})
    title, summary = label, guide
    if label == '혼조 / 변화 관찰':
        present = [d for d in directions if d is not None]
        reason = ('방향 엇갈림' if 1 in present and -1 in present else
                  '기준 이상 움직임 부족' if present and all(d == 0 for d in present) else '일부 지표 기준 미달')
        if 1 in present and -1 in present and 0 in present:
            reason += ' · 일부 지표 기준 미달'
        if None in directions:
            reason += ' · 일부 자료 부족'
        title, summary = '동조 조건 미충족', reason
    elif label == '현물·선물 엇갈림':
        title, summary = '방향 엇갈림', '외국인 선물과 %s의 최근 5분 방향이 반대입니다. 거래 목적은 식별할 수 없습니다.' % NAMES[cash_key]
    elif label in ('현물 매수 유입', '현물 매도 유입'):
        title = NAMES[cash_key] + (' 매수 유입' if directions[1] == 1 else ' 매도 유입')
        summary = '%s과 KOSPI200의 동조입니다. 외국인 선물은 자료 부족으로 확인하지 못했습니다.' % NAMES[cash_key]
    def item(name, value, unit, key):
        return {'name': name, 'value': value, 'unit': unit, 'asof': engine.values.get(key, {}).get('at'),
                'source': engine.values.get(key, {}).get('source', '')}
    day_window = next(w for w in price_oi['windows'] if w['key'] == 'day')
    day = [item('선물 전일 대비', day_window['price_pct'], '%', 'fut_price'),
           item('외국인 현물 당일 누적', metrics['foreign_spot_amt']['day'], '억원', 'foreign_spot_amt'),
           item('외국인 일반선물 당일 누적', metrics['foreign_fut_qty']['day'], '계약', 'foreign_fut_qty'),
           item('상승 종목 비율 · KRX 코스피', engine.val('breadth', at), '%', 'breadth')]
    day[-1]['absolute'] = True
    price, change = engine.val('k200', at), metrics['k200']['m15']
    pct15 = change/(price-change)*100 if change is not None and price and price-change > 0 else None
    context15 = [item('외국인 선물 15분', metrics['foreign_fut_qty']['m15'], '계약', 'foreign_fut_qty'),
                 item(NAMES[cash_key]+' 15분', metrics[cash_key]['m15'], '억원', cash_key),
                 item('외국인 현물 15분', metrics['foreign_spot_amt']['m15'], '억원', 'foreign_spot_amt'),
                 item('KOSPI200 15분', pct15, '%', 'k200')]
    if cash_key == 'foreign_spot_amt':
        context15.pop(2)
    reference = [item('시장−이론 베이시스 현재', metrics['basis_residual']['day'], 'p', 'basis_residual'),
                 item('시장−이론 베이시스 5분 변화', metrics['basis_residual']['m5'], 'p', 'basis_residual'),
                 item('선물가격 15분', price_oi['windows'][1]['price_pct'], '%', 'fut_price'),
                 item('시장 전체 미결제 15분 변화', metrics['fut_oi']['m15'], '계약', 'fut_oi')]
    speed_keys = list(dict.fromkeys((cash_key, 'foreign_spot_amt')))
    return {'asof': stamp(at).isoformat(), 'day': day, 'm5': {'title': title, 'summary': summary, 'axes': axes},
            'm15': context15, 'speeds': [flow_speed(engine, k, at) for k in speed_keys], 'reference': reference,
            'reference_note': '베이시스·15분 가격·미결제는 현재 5분 동조 판정에 직접 반영되지 않는 참고 지표입니다. 미결제는 시장 전체 값이며 외국인의 신규·청산 여부는 식별할 수 없습니다.'}
