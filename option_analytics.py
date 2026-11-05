"""옵션 투자자 수량의 델타 환산과 행사가별 미결제 구조(벽·max pain).

입력은 모두 저장된 조회 응답이다(opt50038 투자자, opt50021/22 시세). 실시간 틱이 아니며
투자자 보유량을 뜻하지 않는다. 결측은 0으로 바꾸지 않고 누락 건수로 드러낸다.
"""
import datetime as dt
import json
import math
from pathlib import Path
from common import KST,ROOT,expected_days,in_session,last_trading_day,monthly_expiry,number,stamp
from investor_probe import RECORD

HISTORY='option_flow_history.jsonl'
FUT_KEYS={'foreign':'foreign_fut_qty','individual':'individual_fut_qty','financial':'financial_fut_qty'}
PARTS=('call_buy','call_sell','put_buy','put_sell')


def expiry_at(month,cfg):
    hh,mm=map(int,cfg['calendar']['expiry_cutoff'].split(':'))
    return dt.datetime.combine(monthly_expiry(month,cfg),dt.time(hh,mm),tzinfo=KST)


def years_left(month,at,cfg):
    return max((expiry_at(month,cfg)-stamp(at)).total_seconds(),60)/(365*86400)


def bs_delta(cp,spot,strike,iv_pct,years):
    if not spot or not strike or not iv_pct or iv_pct<=0:return None
    sigma=iv_pct/100
    d1=(math.log(spot/strike)+.5*sigma*sigma*years)/(sigma*math.sqrt(years))
    call=.5*(1+math.erf(d1/math.sqrt(2)))
    return call if cp=='C' else call-1


def load_quotes(directory):
    quotes={};meta={}
    for kind in ('calls','puts'):
        snapshot=json.loads((Path(directory)/('snapshot_'+kind+'.json')).read_text(encoding='utf-8'))['snapshot']
        meta[kind]=snapshot['received_at']
        # 연속 조회 도입 전 파일엔 complete가 없다 → 200행 상한에 걸렸을 수 있으므로 불완전으로 본다.
        # 서버의 '다음 쪽 없음'만으로는 부족하다. 상장 행사가 대조(listed_count)를 거친 경우만 완전으로 본다.
        meta[kind+'_complete']=bool(snapshot.get('complete')) and snapshot.get('listed_count') is not None
        meta[kind+'_listed']=snapshot.get('listed_range')
        for row in snapshot['rows']:quotes[row['code']]=row
    return quotes,meta


def deltas(row,spot,years):
    raw=number(row.get('delta_raw'))
    # 키움 델타는 % 단위(ATM 콜 54.6 + 풋 -45.4 = 100, 2026-10-15 확인).
    return {'kiwoom':raw/100 if raw is not None else None,'bs':bs_delta(row['cp'],spot,row['strike'],number(row.get('iv')),years)}


def delta_flows(report,quotes,spot,years):
    """실제 0과 결측을 구분한다(검토 2번). 계산 가능한 행이 없으면 합계는 None, 일부 누락이면 partial."""
    groups=[]
    for result in report.get('results',[]):
        if result.get('scope')!='daily_net_trades':continue
        sums={m:{p:0.0 for p in PARTS} for m in ('kiwoom','bs')}
        contracts={p:0.0 for p in PARTS};matched=missing_delta=missing_qty=unmatched=0
        qty_map={};delta_map={}
        response=result.get('response')
        for raw in (response or {}).get(RECORD,[]):
            for cp,prefix in (('C',''),('P','풋_')):
                code=str(raw.get(prefix+'종목코드','')).strip()
                if not code:continue  # GetRepeatCnt=0일 때 생기는 빈 행
                quote=quotes.get(code)
                if not quote or quote.get('cp')!=cp or number(raw.get('행사가'))!=number(quote.get('strike')):
                    unmatched+=1;continue
                qty=number(raw.get(prefix+'투자자별순매수수량'))
                if qty is None:
                    missing_qty+=1;continue
                d=deltas(quote,spot,years)
                if any(v is None or abs(v)>1 for v in d.values()):
                    if qty:missing_delta+=1
                    else:matched+=1;qty_map[code]=0
                    continue
                matched+=1;qty_map[code]=qty;delta_map[code]=d
                if qty:
                    part=('call_' if cp=='C' else 'put_')+('buy' if qty>0 else 'sell')
                    contracts[part]+=qty
                    for m in sums:sums[m][part]+=qty*d[m]
        ok=response is not None and matched>0
        partial=bool(missing_qty or missing_delta or unmatched)
        groups.append({'investor':result['investor'],'label':result['label'],'received_at':result.get('received_at'),
            'available':ok,'partial':partial,'message':result.get('message',''),'matched':matched,
            'missing_qty':missing_qty,'missing_delta':missing_delta,'unmatched':unmatched,
            'contracts':{k:round(v) for k,v in contracts.items()} if ok else None,
            'delta':{m:({'total':round(sum(v.values()),1),**{k:round(x,1) for k,x in v.items()}} if ok else None) for m,v in sums.items()},
            '_qty':qty_map,'_delta':delta_map})
    return groups


def max_pain(calls,puts):
    strikes=sorted({r['strike'] for r in calls+puts})
    if not strikes:return None
    def payout(k):
        return sum(r['oi']*max(k-r['strike'],0) for r in calls)+sum(r['oi']*max(r['strike']-k,0) for r in puts)
    return min(strikes,key=payout)


def oi_structure(quotes,spot,cfg,at,complete=True):
    rows=[r for r in quotes.values() if number(r.get('oi')) is not None]
    if not rows or not spot:return None
    month=rows[0]['month']
    calls=[r for r in rows if r['cp']=='C'];puts=[r for r in rows if r['cp']=='P']
    above=[r for r in calls if r['strike']>=spot];below=[r for r in puts if r['strike']<=spot]
    lo,hi=min(r['strike'] for r in rows),max(r['strike'] for r in rows)
    # 조회 범위 끝에 걸친 벽은 범위 밖에 더 큰 미결제가 있을 수 있다.
    top=lambda xs:[{'strike':r['strike'],'oi':r['oi'],'edge':not complete and r['strike'] in (lo,hi)} for r in sorted(xs,key=lambda r:-r['oi'])[:3]]
    call_oi=sum(r['oi'] for r in calls);put_oi=sum(r['oi'] for r in puts)
    # 차트는 저항·지지 후보 탐색 범위(±7%)보다 조금 넓게 보여 준다.
    near={r['strike'] for r in rows if abs(r['strike']-spot)<=spot*.08}
    exp=monthly_expiry(month,cfg);today=stamp(at).date()
    # 선물 장(15:45)이 끝나면 오늘은 남은 거래일에서 뺀다(10/23 마감 뒤 11/12 만기까지는 만기일 포함 14거래일).
    closed=stamp(at).strftime('%H:%M')>='15:45'
    days=len(expected_days(today+dt.timedelta(days=1) if closed else today,exp,cfg))
    days_basis='오늘 제외·만기일 포함' if closed else '오늘·만기일 포함'
    # 2026-10-29: ±5%면 지수 935.87에서 상한 982.66이라 미결제 최대(6,951계약)인 1000이 빠졌다.
    # 행사가별 미결제 변화(deriv_today.oi_view)와 같은 ±7%로 맞춘다. 경계 행사가는 부동소수 오차로 빠지지 않게 포함한다.
    near_band=.07
    return {'month':month,'expiry':exp.isoformat(),'business_days_left':days,'days_basis':days_basis,'expiry_week':days<=5,'spot':spot,
        'near_band_pct':near_band*100,
        'near_call_wall':(top([r for r in above if r['strike']<=spot*(1+near_band)+1e-6]) or [None])[0],
        'near_put_wall':(top([r for r in below if r['strike']>=spot*(1-near_band)-1e-6]) or [None])[0],
        'call_walls':top(above),'put_walls':top(below),'max_pain':max_pain(calls,puts),
        'call_oi':call_oi,'put_oi':put_oi,'pcr_oi':round(put_oi/call_oi,3) if call_oi else None,
        'rows':sorted(({'strike':r['strike'],'cp':r['cp'],'oi':r['oi']} for r in rows if r['strike'] in near),key=lambda r:(r['strike'],r['cp'])),
        'complete':complete,'strike_range':[lo,hi],
        'coverage':('조회된 %d개 콜·%d개 풋(행사가 %g~%g) 기준'%(len(calls),len(puts),lo,hi))+(' · 상장 행사가 전부 수신' if complete else ' · 상장 행사가 일부 미수신, max pain·끝단 벽은 참고 불가')}


def read_history(path,day):
    out=[]
    try:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            try:entry=json.loads(line)
            except ValueError:continue
            if entry.get('generated_at','')[:10]==day:out.append(entry)
    except OSError:pass
    return out


def decompose(now,base):
    """직전 기록 대비 변화 = 매매분(수량 변화 × 현재 델타) + 델타 재평가(나머지). 검토 5번.

    매매분도 체결 시점 델타가 아니라 현재 델타로 환산한 근사다. 두 기록 중 하나라도
    불완전(partial/없음)하면 None.
    """
    if not now or not base:return None
    out={}
    for m in ('kiwoom','bs'):
        total_now,total_base=now['totals'].get(m),base['totals'].get(m)
        if total_now is None or total_base is None:return None
        trade=sum((q-base['qty'].get(code,0))*now['delta'][code][m] for code,q in now['qty'].items() if code in now['delta'])
        out[m]={'total':round(total_now-total_base,1),'trade':round(trade,1),'revalue':round(total_now-total_base-trade,1)}
    return out


def history_record(group):
    if not group['available'] or group['partial']:return None
    return {'totals':{m:v['total'] for m,v in group['delta'].items()},'qty':group['_qty'],
            'delta':{c:{m:round(v,6) for m,v in d.items()} for c,d in group['_delta'].items()}}


def prior_entry(history,at,minutes):
    """at보다 minutes분 이상 앞선 가장 가까운 기록. 5분 주기라 허용 오차 5분."""
    target=at-dt.timedelta(minutes=minutes)
    prior=[h for h in history if stamp(h['generated_at'])<=target and (target-stamp(h['generated_at'])).total_seconds()<=300]
    return prior[-1] if prior else None


PART_WORDS={'call_buy':'콜 매수','call_sell':'콜 매도','put_buy':'풋 매수','put_sell':'풋 매도'}


def range_text(a,b):
    lo,hi=sorted((abs(a),abs(b)))
    return '%s계약'%format(round(lo),',') if round(lo)==round(hi) else '%s~%s계약'%(format(round(lo),','),format(round(hi),','))


def judge(a,b,flat):
    """두 방식 값의 방향 판정: None=값 없음, 0=작음, 'split'=방향 엇갈림, ±1."""
    if a is None or b is None:return None
    if max(abs(a),abs(b))<flat:return 0
    if (a>0)!=(b>0) or a==0 or b==0:return 'split'
    return 1 if a>0 else -1


def explain(group,flat=50,when='오늘'):
    """델타 두 방식이 같은 방향이고 누락이 없을 때만 방향을 말한다."""
    label=group['label']
    if not group['available']:
        return {'side':None,'text':'%s: 데이터 없음 · %s'%(label,group['message'] or '계산 가능한 행이 없습니다')}
    if group['partial']:
        return {'side':None,'text':'%s: 일부 행 누락(수량 %d·델타 %d·시세 불일치 %d)으로 방향 해석 보류'%(
            label,group['missing_qty'],group['missing_delta'],group['unmatched'])}
    d=group['delta'];a,b=d['kiwoom']['total'],d['bs']['total']
    j=judge(a,b,flat)
    if j==0:return {'side':0,'text':'%s: 뚜렷한 방향 없음 (선물 %s 상당 이내)'%(label,range_text(a,b))}
    if j=='split':return {'side':None,'text':'%s: 델타 계산 방식에 따라 방향이 엇갈림(키움 %+.0f / BS %+.0f) · 해석 보류'%(label,a,b)}
    main=max(PARTS,key=lambda p:abs(d['kiwoom'][p]))
    return {'side':j,'text':'%s: %s 옵션으로 %s 쪽 (선물 %s %s 효과, 주로 %s)'%(
        label,when,'상승' if j>0 else '하락',range_text(a,b),'매수' if j>0 else '매도',PART_WORDS[main])}


def combined_text(group,fut,flat):
    """선물(0780)+옵션 합산. 두 방식 부호가 다르면 단정하지 않는다(검토 4번)."""
    c=group['combined']
    head='0780 선물 %+d계약(%s 입력) + 조회된 옵션 행사가 범위 합계'%(round(fut['value']),fut['at'][11:16])
    j=judge(c['kiwoom'],c['bs'],flat)
    if j==0:return head+': 뚜렷한 방향 없음'
    if j=='split':return head+': 키움 %+.0f / BS %+.0f, 방향 엇갈림 · 해석 보류'%(c['kiwoom'],c['bs'])
    return head+': 선물 %s %s 상당'%(range_text(c['kiwoom'],c['bs']),'매수' if j>0 else '매도')


def structure_text(st):
    if not st:return ''
    def wall(w,word):
        if not w:return word+' 없음'
        return '%s %g (미결제 %s)'%(word,w['strike'],format(round(w['oi']),','))
    parts=['지수 %.1f'%st['spot'],wall(st['near_call_wall'],'지수 위 콜 미결제 최대'),wall(st['near_put_wall'],'지수 아래 풋 미결제 최대')]
    if st.get('max_pain') is not None and st.get('complete'):parts.append('max pain %g(참고 · KOSPI200 실증 근거 미확인)'%st['max_pain'])
    elif st.get('max_pain') is not None:parts.append('max pain은 먼 행사가 미수신으로 표시 보류')
    parts.append('만기 %s · 남은 %d거래일(%s)%s'%(st['expiry'][5:].replace('-','/'),st['business_days_left'],st.get('days_basis','만기일 포함'),' · 만기 주간' if st['expiry_week'] else ''))
    return ' · '.join(parts)


def ls_totals(directory,quotes_at):
    """LS t1602 시장구분 5·6 옵션 투자자 합계. 시세 조회와 10분 안일 때만 같은 회차로 붙인다."""
    from ls_deriv import load_report
    report=load_report(directory)
    if not report:return None
    if abs((stamp(report['generated_at'])-quotes_at).total_seconds())>600:return None
    return report


def ls_view(report,mode):
    """화면용 한 줄씩. 콜·풋 구분이 확인되기 전에는 방향(상승·하락) 말을 붙이지 않는다."""
    if not report:return None
    from ls_deriv import MAPPING_CONFIRMED,OPTION_MARKETS,market_label
    by_market={m['market']:m for m in OPTION_MARKETS}
    rows=[]
    for g in report['groups']:
        # 확인 전에 저장된 파일도 지금 확인된 콜·풋 이름으로 다시 붙인다(2026-10-22: 5=풋, 6=콜).
        g={**g,'label':market_label(by_market[g['market']])} if g.get('market') in by_market else g
        q,a=g.get('qty') or {},g.get('amt') or {}
        parts=['%s %s계약'%(label,'{:+,}'.format(round(q[key]))) for key,label in report['investors'] if key in q and key!='financial']
        rows.append({**g,'text':'%s: %s'%(g['label'],' · '.join(parts) or '값 없음'),
                     'amt_text':' · '.join('%s %+.0f억'%(label,a[key]) for key,label in report['investors'] if key in a and key!='financial')})
    confirmed=report.get('mapping_confirmed') or MAPPING_CONFIRMED
    return {'generated_at':report['generated_at'],'data_day':report.get('data_day'),'source':report['source'],
            'confirmed':bool(confirmed),'rows':rows,
            'headline':'옵션 투자자 합계(LS) · '+('콜·풋 구분 확인됨' if confirmed else '콜·풋 구분 미확정 → 숫자만 보고 방향은 판단하지 않습니다'),
            # 확인 전에 저장된 파일의 옛 설명('콜·풋 구분 미확정', '옵션 단위 대조 전')은 지금 확인된 설명으로 바꾼다.
            'note':(__import__('ls_deriv').NOTE if confirmed else report.get('note',''))}


def freshness(generated,at,cfg,poll=None):
    """live: 장중·당일·주기 안 / stale: 장중인데 조회가 늦음 / archive: 장외 보관본. 검토 1번."""
    poll=poll or (cfg or {}).get('ocx',{}).get('option_investor_poll_sec',300)
    limit=poll*2+120
    age=(at-generated).total_seconds()
    if in_session(at,cfg) and generated.date()==at.date() and 0<=age<=limit:return 'live',age,limit
    if in_session(at,cfg):return 'stale',age,limit
    return 'archive',age,limit


def build(directory=None,spot=None,spot_source='',fut=None,cfg=None,at=None,persist_history=True):
    directory=Path(directory or ROOT/'data');at=stamp(at or dt.datetime.now(KST))
    flat=(cfg or {}).get('rules',{}).get('option_delta_flat',50)
    try:quotes,meta=load_quotes(directory)
    except (OSError,ValueError,KeyError):
        return {'status':'unavailable','note':'LS(또는 키움) 수집기가 옵션 시세를 조회하면 표시됩니다.'}
    quotes_at=max(stamp(meta[k]) for k in ('calls','puts'))
    try:report=json.loads((directory/'investor_options.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):report=None
    # 키움 행사가별 투자자 수량(opt50038)은 같은 조회 회차(10분 이내) 시세가 있을 때만 델타 환산한다.
    # LS로 바꾼 뒤에는 이 파일이 갱신되지 않아 자연히 빠지고, 시세·OI 구조만 계산한다.
    if report and max(abs((stamp(meta[k])-stamp(report['generated_at'])).total_seconds()) for k in ('calls','puts'))>600:report=None
    totals=ls_totals(directory,quotes_at)
    if report is None:
        report={'generated_at':quotes_at.isoformat(),'results':[],
                'data_day':(totals or {}).get('data_day') or last_trading_day(quotes_at,cfg).isoformat()}
        poll=(cfg or {}).get('ls',{}).get('option_poll_sec',300)
    else:poll=None
    generated=stamp(report['generated_at']);day=report['generated_at'][:10]
    mode,age,limit=freshness(generated,at,cfg,poll)
    if mode=='live' and report.get('data_day') and report['data_day']!=at.date().isoformat():
        # 08:45~09:05 첫 조회는 직전 거래일 일자로 받는다. 방금 받은 값이어도 '오늘'이 아니다(검토 보완 4).
        mode='prior'
    if mode!='live' or not spot:
        # 장중 최신 조회가 아니면 실시간 지수를 섞지 않고 조회 시점 지수를 쓴다.
        try:
            snap=json.loads((directory/'snapshot_futures.json').read_text(encoding='utf-8'))['snapshot']
            spot=number(snap['values'].get('k200'));spot_source='선물 조회 시점 KOSPI200 (%s)'%snap['received_at'][5:16].replace('T',' ')
        except (OSError,ValueError,KeyError):spot=None
    if not spot:return {'status':'unavailable','note':'KOSPI200 기준값이 없어 델타를 계산할 수 없습니다.'}
    month=next(iter(quotes.values()))['month']
    years=years_left(month,report['generated_at'],cfg)
    groups=delta_flows(report,quotes,spot,years)
    entry={'generated_at':report['generated_at'],'spot':spot,'investors':{g['investor']:history_record(g) for g in groups}}
    path=directory/HISTORY
    history=read_history(path,day)
    if persist_history and groups and (not history or history[-1]['generated_at']!=entry['generated_at']):
        with open(path,'a',encoding='utf-8') as f:f.write(json.dumps(entry,ensure_ascii=False)+'\n')
        history.append(entry)
    prev=[h for h in history if h['generated_at']<entry['generated_at']]
    base15=prior_entry(history,generated,15)
    data_day=report.get('data_day') or day
    md=lambda s:s[5:10].replace('-','/')
    if mode=='live':when='오늘'
    elif data_day!=day:when='%s 거래일(%s %s 조회) 기준 그날'%(md(data_day),md(day),report['generated_at'][11:16])
    else:when='%s %s 기준 그날'%(md(data_day),report['generated_at'][11:16])
    fut=fut or {}
    for g in groups:
        inv=g['investor'];now=entry['investors'].get(inv)
        g['change_prev']=decompose(now,(prev[-1].get('investors') or {}).get(inv)) if prev else None
        g['change_15m']=decompose(now,(base15.get('investors') or {}).get(inv)) if base15 else None
        g['explain']=explain(g,flat,when)
        if mode=='stale':
            g['explain']={'side':None,'text':'%s: 마지막 조회가 %d분 전(%s)이라 방향 해석 보류'%(g['label'],age//60,report['generated_at'][11:16])}
        f=fut.get(FUT_KEYS.get(inv,''))
        # 합산은 장중 최신 조회이고 0780 입력이 옵션 조회와 10분 안일 때만.
        if f and mode=='live' and g['available'] and not g['partial'] and abs((stamp(f['at'])-generated).total_seconds())<=600:
            g['futures']=f
            g['combined']={m:round(f['value']+v['total'],1) for m,v in g['delta'].items()}
            g['explain']['combined_text']=combined_text(g,f,flat)
        else:
            g['futures']=f if f else None;g['combined']=None
        g.pop('_qty',None);g.pop('_delta',None)
    structure=oi_structure(quotes,spot,cfg,at if mode=='live' else quotes_at,complete=meta['calls_complete'] and meta['puts_complete'])
    notes={'live':'','stale':'장중이지만 마지막 조회가 %d분 넘게 지났습니다(기준 %d분). 수집기 상태를 확인하세요.'%(age//60,limit//60),
           'archive':'장외 보관본입니다. 실시간 판단에 쓰지 않습니다.',
           'prior':'장 시작 직후라 직전 거래일(%s) 옵션 수급입니다. 오늘 값은 09:05 이후 조회부터 나옵니다.'%(report.get('data_day') or '')}
    return {'status':'received','mode':mode,'mode_note':notes[mode],'data_day':data_day,'structure_text':structure_text(structure),
        'generated_at':report['generated_at'],'quotes_at':meta,'spot':spot,'spot_source':spot_source,
        'methodology':{'id':'oi-structure-2','near_band_pct':round(structure.get('near_band_pct',7),4),
                       'month':month,'reference_spot':spot,'quotes_at':meta,'scope':'조회 월물 · 지수 위 콜 / 아래 풋'},
        'years_to_expiry':round(years,4),'previous_at':prev[-1]['generated_at'] if prev else None,
        'flows':groups,'structure':structure,'ls_totals':ls_view(totals,mode),
        'note':'옵션 투자자는 LS 당일 순매수 합계만 있습니다(행사가별 수량 없음 → 델타 환산 안 함). OI 구조는 상장 행사가 전체 시세 기준입니다.' if not groups else'옵션 당일 순매매 × 델타 = 선물 계약 상당 노출의 근사입니다. 보유 포지션이 아니며, 조회된 월물·행사가 범위만 포함합니다. 변화는 매매분(수량 변화×현재 델타)과 델타 재평가(가격 변화로 같은 수량의 환산값이 바뀐 부분)로 나눕니다.'}
