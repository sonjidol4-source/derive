"""한투 REST 장중 파생 자료 1회 점검(조회 전용).

- 시장별 투자자매매동향(시세) FHPTJ04030000: 선물·콜·풋·미니·위클리를 이름이 붙은 시장코드로 조회
  (공식 Postman 명세: K2I+F001 선물, OC01 콜옵션, OP01 풋옵션 / MKI+F004·OC02·OP02 / WKM+OC05·OP05 / WKI+OC04·OP04)
- 국내옵션전광판 옵션월물리스트 FHPIO056104C0, 콜풋 FHPIF05030100(행사가별 미결제·증감·그릭스), 선물 FHPIF05030200
결과: data/kis_probe.json(원문), data/한투_장중점검.txt(요약 + LS 저장본 대조). 키·토큰은 쓰지 않는다.
"""
import json
import sys

import kis_api as kis
from common import ROOT, atomic_json, config, nearest_month, now_kst, number

DATA = ROOT / 'data'
INVESTOR_PATH = '/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market'
INVESTOR_TR = 'FHPTJ04030000'
INVESTOR_QUERIES = [
    ('futures', '선물', 'K2I', 'F001'), ('call', '콜옵션', 'K2I', 'OC01'), ('put', '풋옵션', 'K2I', 'OP01'),
    ('mini_futures', '미니선물', 'MKI', 'F004'), ('mini_call', '미니 콜', 'MKI', 'OC02'), ('mini_put', '미니 풋', 'MKI', 'OP02'),
    ('wk_mon_call', '위클리(월) 콜', 'WKM', 'OC05'), ('wk_mon_put', '위클리(월) 풋', 'WKM', 'OP05'),
    ('wk_thu_call', '위클리(목) 콜', 'WKI', 'OC04'), ('wk_thu_put', '위클리(목) 풋', 'WKI', 'OP04'),
]
INVESTORS = [('frgn', '외국인'), ('prsn', '개인'), ('orgn', '기관계'), ('scrt', '금융투자')]
BOARD_PATH = '/uapi/domestic-futureoption/v1/quotations/'


def investor_values(row):
    out = {}
    for code, _ in INVESTORS:
        q = number(row.get(code + '_ntby_qty'))
        a = number(row.get(code + '_ntby_tr_pbmn'))
        out[code] = {'qty': q, 'amt_raw': a}
    return out


def first_row(body, key='output'):
    v = body.get(key)
    if isinstance(v, list):
        return v[0] if v else {}
    return v or {}


def board_summary(rows, label):
    items = []
    for r in rows:
        strike = number(r.get('acpr'))
        if strike is None:
            continue
        items.append({'strike': strike, 'oi': number(r.get('hts_otst_stpl_qty')), 'oi_chg': number(r.get('otst_stpl_qty_icdc')),
                      'price': number(r.get('optn_prpr')), 'vol': number(r.get('acml_vol')), 'iv': number(r.get('hts_ints_vltl')),
                      'delta': number(r.get('delta_val')), 'atm': str(r.get('atm_cls_name') or '').strip()})
    lines = ['%s %d개 행사가' % (label, len(items))]
    by_oi = sorted([i for i in items if i['oi']], key=lambda i: -i['oi'])[:5]
    lines.append('  미결제 상위: ' + ', '.join('%g(%s)' % (i['strike'], format(int(i['oi']), ',')) for i in by_oi))
    by_chg = sorted([i for i in items if i['oi_chg']], key=lambda i: -i['oi_chg'])[:5]
    lines.append('  미결제 증가 상위: ' + ', '.join('%g(%+d)' % (i['strike'], int(i['oi_chg'])) for i in by_chg))
    by_drop = sorted([i for i in items if i['oi_chg']], key=lambda i: i['oi_chg'])[:3]
    lines.append('  미결제 감소 상위: ' + ', '.join('%g(%+d)' % (i['strike'], int(i['oi_chg'])) for i in by_drop))
    atm = [i for i in items if i['atm']]
    if atm:
        lines.append('  ATM 표시 행: ' + ', '.join('%g %s δ%s IV%s' % (i['strike'], i['atm'], i['delta'], i['iv']) for i in atm[:3]))
    return items, lines


def ls_compare(kis_inv):
    """LS 저장본(오늘 마감 뒤 조회)과 한투 값을 계약 수로 대조."""
    lines = []
    try:
        days = json.loads((DATA / 'close_summary.json').read_text(encoding='utf-8'))['days']
        rec = days[sorted(days)[-1]]
        fut = kis_inv.get('futures', {}).get('values') or {}
        pairs = [('외국인', '17', 'frgn'), ('개인', '08', 'prsn'), ('기관계', '18', 'orgn'), ('금융투자', '01', 'scrt')]
        lines.append('선물 대조(%s LS t1602 %s vs 한투): ' % (rec['day'], rec.get('status', '')) + ' / '.join(
            '%s LS %+d · 한투 %s' % (n, rec['qty'].get(c, 0), fmt_q((fut.get(k) or {}).get('qty'))) for n, c, k in pairs))
    except (OSError, ValueError, KeyError, IndexError) as e:
        lines.append('선물 대조: LS 마감 요약 없음(%s)' % type(e).__name__)
    try:
        ls = json.loads((DATA / 'ls_option_investors.json').read_text(encoding='utf-8'))
        groups = {g['market']: g for g in ls['groups']}
        call = (kis_inv.get('call', {}).get('values') or {})
        put = (kis_inv.get('put', {}).get('values') or {})
        names = [('foreign', 'frgn'), ('inst', 'orgn'), ('individual', 'prsn'), ('financial', 'scrt')]

        def agree(g, side):
            return sum(1 for ls_k, k in names if (side.get(k) or {}).get('qty') is not None
                       and g['qty'].get(ls_k) is not None and abs(g['qty'][ls_k] - side[k]['qty']) < 0.5)

        m5, m6 = groups.get('5'), groups.get('6')
        if m5 and m6:
            a = agree(m5, call) + agree(m6, put)
            b = agree(m5, put) + agree(m6, call)
            lines.append('옵션 대조(LS %s 조회): 시장구분5=콜·6=풋 가정 일치 %d/8, 반대 가정 일치 %d/8' % (ls['generated_at'][:16], a, b))
            for label, g in (('LS 5', m5), ('LS 6', m6)):
                lines.append('  %s: ' % label + ' / '.join('%s %+d' % (n, g['qty'].get(k, 0)) for k, n in
                                                           (('foreign', '외국인'), ('inst', '기관계'), ('individual', '개인'), ('financial', '금융투자'))))
            if a == 8 and b < 8:
                lines.append('  → LS 시장구분 5가 콜, 6이 풋으로 확인됨(한투 이름표 기준)')
            elif b == 8 and a < 8:
                lines.append('  → LS 시장구분 5가 풋, 6이 콜(지금 추정과 반대)')
            else:
                lines.append('  → 완전히 일치하지 않음. 조회 시각·범위(위클리·미니 포함 여부) 차이 확인 필요')
    except (OSError, ValueError, KeyError) as e:
        lines.append('옵션 대조: LS 옵션 저장본 없음(%s)' % type(e).__name__)
    return lines


def fmt_q(v):
    return '-' if v is None else '%+d' % v


def main():
    import requests
    cfg = config()
    started = now_kst()
    lines = ['한투 장중 파생 자료 점검 · %s' % started.strftime('%Y-%m-%d %H:%M:%S')]
    out = {'generated_at': started.isoformat(), 'investor': {}, 'errors': []}

    def say(t):
        print(t, flush=True)
        lines.append(t)

    path = kis.credentials_path(cfg)
    try:
        key, secret = kis.load_credentials(path)
        rest = kis.Rest(requests, key, secret, path.parent / 'kis_token.json')
    except kis.KisError as e:
        say('중단: %s' % e)
        (DATA / '한투_장중점검.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return 1
    say('접근토큰: %s' % ('새로 발급(알림톡이 갈 수 있음) · 하루 동안 다시 씀' if rest.issued else '저장본 사용'))

    say('')
    say('[시장별 투자자 순매수 · 수량(계약) / 금액(원문 단위)]')
    for key_, label, iscd, iscd2 in INVESTOR_QUERIES:
        try:
            body = rest.get(INVESTOR_PATH, INVESTOR_TR, {'FID_INPUT_ISCD': iscd, 'FID_INPUT_ISCD_2': iscd2})
            row = first_row(body)
            vals = investor_values(row)
            out['investor'][key_] = {'label': label, 'iscd': iscd, 'iscd2': iscd2, 'values': vals, 'row': row,
                                     'rows': len(body.get('output') or []) if isinstance(body.get('output'), list) else 1}
            say('%-12s ' % label + ' · '.join('%s %s (%s)' % (n, fmt_q(vals[c]['qty']),
                                                              '-' if vals[c]['amt_raw'] is None else format(int(vals[c]['amt_raw']), '+,'))
                                              for c, n in INVESTORS))
        except kis.KisError as e:
            out['errors'].append(str(e))
            say('%-12s 오류: %s' % (label, e))

    say('')
    say('[LS 저장본과 대조]')
    for t in ls_compare(out['investor']):
        say(t)

    say('')
    say('[옵션 전광판 · 행사가별 미결제(전일 대비 증감)]')
    month = None
    try:
        body = rest.get(BOARD_PATH + 'display-board-option-list', 'FHPIO056104C0',
                        {'FID_COND_SCR_DIV_CODE': '509', 'FID_COND_MRKT_DIV_CODE': '', 'FID_COND_MRKT_CLS_CODE': ''})
        out['option_list'] = body.get('output')
        months = [str(r.get('mtrt_yymm') or '').strip() for r in (body.get('output') or [])]
        say('월물 목록: ' + ', '.join(m for m in months if m)[:200])
        say('월물 목록 원문 앞 6행: ' + json.dumps((body.get('output') or [])[:6], ensure_ascii=False))
        out['weekly'] = {}
        for cls, word in (('WKI', '위클리(목)'), ('WKM', '위클리(월)')):
            codes = [str(r.get('mtrt_yymm_code') or '').strip() for r in (body.get('output') or [])]
            for cand in [c for c in codes if c][:3]:
                try:
                    wb = rest.get_all(BOARD_PATH + 'display-board-callput', 'FHPIF05030100',
                                      {'FID_COND_MRKT_DIV_CODE': 'O', 'FID_COND_SCR_DIV_CODE': '20503', 'FID_MRKT_CLS_CODE': 'CO',
                                       'FID_MTRT_CNT': cand, 'FID_MRKT_CLS_CODE1': 'PO', 'FID_COND_MRKT_CLS_CODE': cls},
                                      ('output1', 'output2'), pages=2)
                    n1 = len(wb.get('output1') or [])
                    say('%s 전광판 FID_MTRT_CNT=%s → 콜 %d행, 첫 행사가 %s' % (word, cand, n1, (wb.get('output1') or [{}])[0].get('acpr')))
                    out['weekly'][cls + ':' + cand] = n1
                except kis.KisError as e:
                    say('%s 전광판 FID_MTRT_CNT=%s 오류: %s' % (word, cand, e))
        month = nearest_month([m for m in months if len(m) == 6 and m.isdigit()], started, cfg)
    except kis.KisError as e:
        out['errors'].append(str(e))
        say('월물 목록 오류: %s' % e)
    if not month:
        y, m = started.year, started.month
        month = nearest_month(['%04d%02d' % (y + (m + i - 1) // 12, (m + i - 1) % 12 + 1) for i in range(3)], started, cfg)
    try:
        body = rest.get_all(BOARD_PATH + 'display-board-callput', 'FHPIF05030100',
                            {'FID_COND_MRKT_DIV_CODE': 'O', 'FID_COND_SCR_DIV_CODE': '20503', 'FID_MRKT_CLS_CODE': 'CO',
                             'FID_MTRT_CNT': month, 'FID_MRKT_CLS_CODE1': 'PO', 'FID_COND_MRKT_CLS_CODE': ''},
                            ('output1', 'output2'))
        say('콜풋 전광판 %d쪽 받음(마지막 tr_cont=%r)' % (body['_pages'], rest.last_tr_cont))
        calls, cl = board_summary(body.get('output1') or [], '%s 콜' % month)
        puts, pl = board_summary(body.get('output2') or [], '%s 풋' % month)
        out['board'] = {'month': month, 'calls': calls, 'puts': puts, 'raw_first': {'call': (body.get('output1') or [{}])[0],
                                                                                  'put': (body.get('output2') or [{}])[0]}}
        for t in cl + pl:
            say(t)
    except kis.KisError as e:
        out['errors'].append(str(e))
        say('콜풋 전광판 오류: %s' % e)
    try:
        body = rest.get(BOARD_PATH + 'display-board-futures', 'FHPIF05030200',
                        {'FID_COND_MRKT_DIV_CODE': 'F', 'FID_COND_SCR_DIV_CODE': '20503', 'FID_COND_MRKT_CLS_CODE': ''})
        rows = body.get('output') or []
        out['futures_board'] = rows
        for r in rows[:4]:
            say('선물 %s %s 현재 %s · 미결제 %s' % (r.get('futs_shrn_iscd'), r.get('hts_kor_isnm'), r.get('futs_prpr'), r.get('hts_otst_stpl_qty')))
    except kis.KisError as e:
        out['errors'].append(str(e))
        say('선물 전광판 오류: %s' % e)

    say('')
    say('오류 %d건. 원문은 data/kis_probe.json' % len(out['errors']))
    atomic_json(DATA / 'kis_probe.json', out)
    (DATA / '한투_장중점검.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
