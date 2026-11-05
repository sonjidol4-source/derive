"""Experimental rebound observations. Conditions describe co-occurrence, not causality."""
import datetime as dt
from common import stamp, in_session
from flow_explanation import flow_speed


def rebound_view(engine, at, metrics, price5):
    at = stamp(at)
    cfg = engine.cfg['rules']
    f5, f15 = (metrics['foreign_fut_qty'][w] for w in ('m5','m15'))
    basis = metrics['basis_residual']['m5']
    program, spot = (flow_speed(engine, key, at) for key in ('nonarb_amt','foreign_spot_amt'))
    def support(speed):
        if not speed['available']:
            return None
        return speed['recent_rate'] >= 0 or (speed['prior_rate'] < 0 and speed['recent_rate'] > speed['prior_rate'])
    price = engine.val('k200', at)
    high = None
    if metrics['k200']['m15'] is not None:
        points = []
        for h in engine.history:
            t = stamp(h['at'])
            row = h['values'].get('k200')
            if at-dt.timedelta(minutes=15) <= t <= at-dt.timedelta(minutes=5) and row and 0 <= (t-stamp(row['at'])).total_seconds() <= engine.cfg['quality']['quote_ttl_sec']:
                points.append(row['value'])
        high = max(points) if points else None
    def condition(name, value, text, stage):
        return {'name':name, 'passed':value, 'text':text, 'stage':stage}
    tests = [
        condition('외국인 선물 5분 순매수', None if f5 is None else f5 > 0,
                  '순매수 > 0계약으로 조짐을 관찰합니다. 기존 ±%s계약은 규모 참고 기준입니다.' % cfg['foreign_fut_5m'], 1),
        condition('KOSPI200 5분 상승', None if price5 is None else price5 >= cfg['price_5m_pct'],
                  '최근 5분 +%.2f%% 이상' % cfg['price_5m_pct'], 1),
        condition('비차익 순매도 둔화·순매수', support(program),
                  program.get('text',program['reason']) if program['available'] else program['reason'], 2),
        condition('베이시스 개선', None if basis is None else basis >= cfg['basis_change_5m'],
                  '시장−이론 베이시스 5분 변화 +%.2fp 이상' % cfg['basis_change_5m'], 2),
        condition('외국인 선물 매수 지속', None if f5 is None or f15 is None else f5 > 0 and f15-f5 > 0,
                  '최근 5분과 그 앞 10분 모두 순매수(두 구간을 겹치지 않게 확인)', 2),
        condition('외국인 현물 순매도 둔화·순매수', support(spot),
                  spot.get('text',spot['reason']) if spot['available'] else spot['reason'], 3),
        condition('직전 단기 고점 회복', None if high is None or price is None else price > high,
                  '최근 5분을 제외한 앞선 10분 KOSPI200 최고 관측값 초과', 3)]
    stage = 0
    for candidate in (1,2,3):
        if all(c['passed'] is True for c in tests if c['stage'] <= candidate):
            stage = candidate
        else:
            break
    active = in_session(at,engine.cfg,signals=True)
    held = 0
    confirmed = False
    if stage >= 2 and active:
        c = engine.rebound_candidate
        if c is None or c['stage'] != stage or (at-c['last']).total_seconds() > 75:
            c = {'stage':stage,'since':at,'last':at,'tokens':set()}
            engine.rebound_candidate = c
        c['last'] = at
        c['tokens'].add(tuple(engine.values.get(k,{}).get('at') for k in ('foreign_fut_qty','nonarb_amt','basis_residual','k200')))
        held = (at-c['since']).total_seconds()/60
        confirmed = held >= cfg['confirm_min'] and len(c['tokens']) >= 3
        if confirmed:
            kind = '반등 지속성 확인' if stage == 3 else '반등 확인'
            facts = [c['name']+': '+c['text'] for c in tests if c['stage'] <= stage]
            observations = engine.observation_inputs(at, ['foreign_fut_qty','nonarb_amt','basis_residual','k200','foreign_spot_amt'], [])
            # Include the 15-minute base used for persistence and the preceding high.
            starts=[stamp(w['base_observed_at']) for windows in getattr(engine,'window_details',{}).values()
                    for name,w in windows.items() if name=='m15' and w.get('status')=='ready']
            start=min(starts,default=at-dt.timedelta(minutes=16))
            for h in engine.history:
                if start <= stamp(h['at']) <= at-dt.timedelta(minutes=5):
                    observations.extend({**row,'field':k,'role':'rebound_15m','history_at':h['at']} for k,row in h['values'].items() if k in ('foreign_fut_qty','nonarb_amt','foreign_spot_amt','k200'))
            engine.observe(kind,1,at,facts,{'input_observations':observations,'window_details':getattr(engine,'window_details',{}),
                'rule_context':{'method':'rebound-1','window_selection':'valid-predecessor-2','stage':stage,'foreign_early_min':0,
                                'price5_min':cfg['price_5m_pct'],'basis5_min':cfg['basis_change_5m'],
                                'confirm_min':cfg['confirm_min'],'high_window':'preceding_5_to_15_minutes'}})
    else:
        engine.rebound_candidate = None
    titles = {0:'반등 조건 미충족',1:'반등 조짐',2:'반등 확인 조건 충족 · 지속 관찰',3:'지속성 조건 충족 · 지속 관찰'}
    title = ('반등 지속성 확인' if stage == 3 else '반등 확인') if confirmed else titles[stage]
    if not active:
        title = '판정 시간 외 · 마지막 관측 참고'
    elif stage == 0 and any(c['passed'] is None for c in tests if c['stage']==1):
        title = '반등 판정 보류 · 5분 자료 부족'
    diagnostics = []
    times = [stamp(h['at']) for h in engine.history if stamp(h['at']) <= at]
    gaps = [(a,b) for a,b in zip(times,times[1:]) if (b-a).total_seconds() > engine.cfg['quality']['max_gap_sec']]
    for key in ('foreign_fut_qty','foreign_spot_amt','nonarb_amt','k200','basis_residual'):
        current = engine.measurement(key,at)
        for window,minutes in (('m5',5),('m15',15)):
            ok = metrics[key][window] is not None
            reason = '계산 가능' if ok else ('현재 자료 미수신·지연' if not current['fresh'] else '현재 수신 정상 · 과거 %d분 기준점·연속 관측 부족(공백·시각·출처·월물 확인)' % minutes)
            detail=getattr(engine,'window_details',{}).get(key,{}).get(window,{})
            if not ok and current['fresh'] and detail.get('reason'):
                reason='현재 수신 정상 · '+detail['reason']
            elif ok and detail.get('fallback_used'):
                reason='계산 가능 · 앞선 유효 기준점 사용(실제 %.2f분 비교)' % detail['actual_minutes']
            relevant = [(a,b) for a,b in gaps if b >= at-dt.timedelta(minutes=minutes)]
            if not ok and current['fresh'] and relevant:
                a,b=relevant[-1]
                reason='현재 수신 정상 · 실제 관측 공백 %s → %s KST' % (a.strftime('%H:%M'),b.strftime('%H:%M'))
            diagnostics.append({'key':key,'window':window,'available':ok,'reason':reason})
    return {'title':title,'stage':stage,'confirmed':confirmed,'active':active,'conditions':tests,
            'held_minutes':held,'confirm_minutes':cfg['confirm_min'],'foreign5':f5,'foreign15':f15,
            'basis5':basis,'prior_high':high,'program_speed':program,'spot_speed':spot,'diagnostics':diagnostics,
            'note':'초기 실험 규칙입니다. 조짐은 선물 순매수 >0을 사용하므로 작은 변화도 포함합니다. 확인 단계는 %d분 유지와 새 관측 3회 이상을 요구합니다. 특정 투자자의 신규 매수·숏커버나 반등 원인은 식별하지 않습니다.' % cfg['confirm_min']}
