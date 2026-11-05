"""과거 이력·위클리 옵션 조회 가능성 1회 점검(조회 전용). 결과: data/이력_점검.txt, data/history_probe.json

- LS t1665 기간별투자자매매추이: market 4(선물)·5(풋)·6(콜), gubun2·gubun3 조합별로 일별 행이 오는지, 10/22 값이 확정치(외국인 +1,552계약 / +3,137억)와 맞는지
- LS t8416 선물 일봉: 미결제약정(openyak) 일별 이력
- LS t2421 미결제약정추이
- 한투 옵션월물리스트를 위클리 시장구분(WKM·WKI)으로 조회하고, 받은 코드로 위클리 전광판 조회
키·토큰은 쓰지 않는다.
"""
import json
import time

import requests

import kis_api as kis
from common import ROOT, atomic_json, config
from ls_probe import BASE, credentials, token

DATA = ROOT / 'data'
out, lines = {}, []


def say(t):
    print(t, flush=True)
    lines.append(t)


def ls_call(access, path, tr, block):
    r = requests.post(BASE + path, timeout=15, json={tr + 'InBlock': block},
                      headers={'content-type': 'application/json; charset=utf-8', 'authorization': 'Bearer ' + access,
                               'tr_cd': tr, 'tr_cont': 'N', 'tr_cont_key': ''})
    time.sleep(1.2)
    try:
        return r.json()
    except ValueError:
        return {'rsp_cd': 'HTTP%s' % r.status_code, 'rsp_msg': r.text[:200]}


def main():
    cfg = config()
    say('이력 점검 · LS t1665 / t8416 / t2421 · 한투 위클리 전광판')
    try:
        access = token(*credentials(cfg))
    except Exception as e:
        say('LS 토큰 실패: %s' % e)
        access = None
    if access:
        for market, upcode, label in (('4', '900', '선물'), ('5', '800', '풋(시장5)'), ('6', '700', '콜(시장6)')):
            for g2 in ('1', '2'):
                for g3 in ('1',):
                    body = ls_call(access, '/stock/chart', 't1665', {'market': market, 'upcode': upcode, 'gubun2': g2, 'gubun3': g3,
                                                                     'from_date': '20260701', 'to_date': '20261022'})
                    rows = body.get('t1665OutBlock1') or []
                    key = 't1665_%s_%s_%s' % (market, g2, g3)
                    out[key] = {'rsp': [body.get('rsp_cd'), body.get('rsp_msg')], 'head': body.get('t1665OutBlock'), 'rows': rows}
                    d1022 = next((r for r in rows if r.get('date') == '20261022'), None)
                    say('%s gubun2=%s gubun3=%s → %s %s · %d행 · 기간 %s~%s · 10/22 외국인 %s 기관 %s 개인 %s' % (
                        label, g2, g3, body.get('rsp_cd'), str(body.get('rsp_msg', ''))[:30], len(rows),
                        rows[-1].get('date') if rows else '-', rows[0].get('date') if rows else '-',
                        d1022 and d1022.get('sv_17'), d1022 and d1022.get('sv_18'), d1022 and d1022.get('sv_08')))
        body = ls_call(access, '/futureoption/chart', 't8416', {'shcode': 'A016C000', 'gubun': '2', 'qrycnt': 80, 'sdate': '20260701',
                                                                 'edate': '20261022', 'cts_date': '', 'comp_yn': 'N'})
        rows = body.get('t8416OutBlock1') or []
        out['t8416'] = {'rsp': [body.get('rsp_cd'), body.get('rsp_msg')], 'rows': rows}
        say('t8416 선물 일봉 → %s · %d행 · 최근: %s' % (body.get('rsp_cd'), len(rows), json.dumps(rows[-1:] or rows[:1], ensure_ascii=False)[:200]))
        for bd in ('1', '2'):
            body = ls_call(access, '/futureoption/market-data', 't2421', {'focode': 'A016C000', 'bdgubun': bd, 'nmin': '1', 'tcgubun': '1', 'cnt': 60})
            rows = body.get('t2421OutBlock1') or []
            out['t2421_' + bd] = {'rsp': [body.get('rsp_cd'), body.get('rsp_msg')], 'head': body.get('t2421OutBlock'), 'rows': rows}
            say('t2421 bdgubun=%s → %s %s · %d행 · 첫 행 %s' % (bd, body.get('rsp_cd'), str(body.get('rsp_msg', ''))[:30], len(rows),
                                                         json.dumps(rows[:1], ensure_ascii=False)[:200]))
    # 한투 위클리
    try:
        path = kis.credentials_path(cfg)
        key, secret = kis.load_credentials(path)
        rest = kis.Rest(requests, key, secret, path.parent / 'kis_token.json', min_gap=0.5)
        board = '/uapi/domestic-futureoption/v1/quotations/'
        for cls in ('WKM', 'WKI', 'MKI'):
            try:
                body = rest.get(board + 'display-board-option-list', 'FHPIO056104C0',
                                {'FID_COND_SCR_DIV_CODE': '509', 'FID_COND_MRKT_DIV_CODE': '', 'FID_COND_MRKT_CLS_CODE': cls})
                lst = body.get('output') or []
                out['optlist_' + cls] = lst
                say('한투 월물리스트 %s → %s' % (cls, json.dumps(lst[:6], ensure_ascii=False)))
            except kis.KisError as e:
                say('한투 월물리스트 %s 오류: %s' % (cls, e))
                lst = []
            cands = [str(r.get('mtrt_yymm') or '').strip() for r in lst][:2] + [str(r.get('mtrt_yymm_code') or '').strip() for r in lst][:2]
            for c in [c for c in dict.fromkeys(cands) if c]:
                for p in (c, c[2:] if len(c) == 8 else None):
                    if not p:
                        continue
                    try:
                        b = rest.get(board + 'display-board-callput', 'FHPIF05030100',
                                     {'FID_COND_MRKT_DIV_CODE': 'O', 'FID_COND_SCR_DIV_CODE': '20503', 'FID_MRKT_CLS_CODE': 'CO',
                                      'FID_MTRT_CNT': p, 'FID_MRKT_CLS_CODE1': 'PO', 'FID_COND_MRKT_CLS_CODE': cls})
                        o1 = b.get('output1') or []
                        out['wboard_%s_%s' % (cls, p)] = o1[:3]
                        strikes = [x.get('acpr') for x in o1]
                        say('  전광판 %s FID_MTRT_CNT=%s → 콜 %d행 · 행사가 %s~%s · 미결제합 %s' % (
                            cls, p, len(o1), strikes[-1] if strikes else '-', strikes[0] if strikes else '-',
                            sum(int(float(x.get('hts_otst_stpl_qty') or 0)) for x in o1)))
                    except kis.KisError as e:
                        say('  전광판 %s %s 오류: %s' % (cls, p, e))
    except kis.KisError as e:
        say('한투 오류: %s' % e)
    atomic_json(DATA / 'history_probe.json', out)
    (DATA / '이력_점검.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    say('저장: data/이력_점검.txt')


if __name__ == '__main__':
    main()
