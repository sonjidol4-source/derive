"""장 마감 후 하루 요약: LS t1602 선물 투자자 최종값과 같은 날 현물·옵션 기록을 날짜별로 저장.

2026-10-16 LS 시계열에서 확인한 사실(15:35 → 15:46 → 16:07 변화):
  외국인 순매수 15:35 -5,104 → 15:46 -6,398 → 16:07 -4,200(증권사 화면·영웅문 0780과 일치).
  즉 마감 직후 값은 확정치와 다를 수 있다. 마지막 변화 뒤 20분 이상 값이 그대로여야 '확정 추정'으로 본다.
LS 시계열은 시각만 주고 날짜는 주지 않는다. 날짜는 거래일 달력으로 붙이고, 달력이 틀리면 날짜가 어긋날 수 있다.
"""
import datetime as dt
import json
import threading
from copy import deepcopy
from pathlib import Path
from common import ROOT,atomic_json,business_day,number
from price_oi import price_oi_quadrant,reading,rules as price_oi_rules
from ls_deriv import relabel

FILE='close_summary.json'
SAVE_LOCK=threading.RLock()
NAMES={'17':'외국인','18':'기관계','08':'개인','01':'금융투자(증권)','03':'투신','04':'은행','02':'보험','05':'종금','06':'기금','07':'기타'}
MAIN=('17','18','08','01')
WEEKDAY='월화수목금토일'
STABLE_MIN=20
SESSION_END='15:50'


def hms(raw):
    s=str(raw or '').strip()
    return '%s:%s:%s'%(s[:2],s[2:4],s[4:6]) if len(s)>=6 and s[:6].isdigit() else None


def sv(row):
    return {k[3:]:row[k] for k in row if k.startswith('sv_') and isinstance(row[k],(int,float)) and not isinstance(row[k],bool)}


def parse_series(rows):
    """LS t1602 시간대 행(최신 먼저) → 최종값·마지막 변화 시각·15:35 시점 값. 형식이 이상하면 ValueError."""
    rows=[r for r in rows or [] if hms(r.get('time'))]
    if not rows:raise ValueError('LS 시간대 행이 없습니다')
    final=sv(rows[0])
    if not any(c in final for c in MAIN):raise ValueError('LS 응답에 투자자 값이 없습니다')
    latest=hms(rows[0]['time']);changed=latest
    for r in rows:
        if sv(r)==final:changed=hms(r['time'])
        else:break
    at_1535=next((sv(r) for r in rows if hms(r['time'])<='15:35:30'),None)
    # 외국인 장중 저점·고점(같은 값이면 가장 늦은 시각). 7/24처럼 −13,460 → −759 되사들인 구간을 따로 보기 위해.
    marks=[(sv(r)['17'],hms(r['time'])) for r in rows if '17' in sv(r)]
    path=None
    if marks:
        lo,hi=min(marks,key=lambda x:x[0]),max(marks,key=lambda x:x[0])
        path={'low':lo[0],'low_time':lo[1],'high':hi[0],'high_time':hi[1]}
    return {'final':final,'latest_time':latest,'last_change':changed,'at_1535':at_1535,'rows':len(rows),'foreign_path':path}


def minutes(hms_text):
    h,m,s=map(int,hms_text.split(':'));return h*60+m+s/60


def status_of(qty,amt):
    """확정 추정 / 마감 직후 잠정 / 장중 값."""
    latest=min(qty['latest_time'],amt['latest_time']);changed=max(qty['last_change'],amt['last_change'])
    if latest<'15:30:00':return '마감 전 값'
    if changed<'15:36:00' and latest<'15:50:00':return '마감 직전 값'
    stable=minutes(latest)-minutes(changed)
    if latest>='16:00:00' and stable>=STABLE_MIN:return '확정 추정'
    return '마감 직후 잠정'


SESSION_START='08:45'
FINAL_TIME='18:15'


def target_day(now,cfg):
    """저장할 거래일(LS '금일'이 가리키는 날). 장중이면 None.

    장 시작 전(08:45 이전)과 휴장일은 직전 거래일. 휴장일에 '금일'=직전 거래일임은 2026-10-17·18에 확인했고,
    평일 장 시작 전에도 같다고 가정한다(미확인). 대신 그 값이 완결된 하루인지(build_record의 상태)로 걸러낸다.
    """
    d=now.date();hm=now.strftime('%H:%M')
    if business_day(d,cfg):
        if hm>=SESSION_END:return d
        if hm<SESSION_START:return previous_business_day(d,cfg)
        return None
    return previous_business_day(d,cfg)


def is_final(record,now,cfg):
    """더 이상 다시 확인하지 않아도 되는가. '확정 추정'이어도 그날 18:15 전이고 LS 시계열이 18시 전이면
    늦은 정정을 놓치지 않도록 계속 확인한다(검토 보완 2)."""
    if not record or record.get('status')!='확정 추정':return False
    if record.get('latest_time','')>='18:00:00':return True
    same_day=now.date().isoformat()==record['day']
    return not (same_day and now.strftime('%H:%M')<FINAL_TIME)


def previous_business_day(d,cfg):
    for _ in range(14):
        d-=dt.timedelta(days=1)
        if business_day(d,cfg):return d
    raise ValueError('이전 거래일을 찾을 수 없습니다')


def build_record(day,qty_rows,amt_rows,asked_at):
    q=parse_series(qty_rows);a=parse_series(amt_rows)
    return {'day':day.isoformat(),'status':status_of(q,a),'asked_at':asked_at,'qty':q['final'],'amt':a['final'],
            'latest_time':min(q['latest_time'],a['latest_time']),'last_change':max(q['last_change'],a['last_change']),
            'at_1535':{'qty':q['at_1535'],'amt':a['at_1535']},'foreign_path':q['foreign_path'],'source':'LS t1602',
            'note':'날짜는 거래일 달력으로 붙였습니다. 계약 기준 판단, 억원은 참고.'}


def load(directory=None):
    try:return json.loads((Path(directory or ROOT/'data')/FILE).read_text(encoding='utf-8')).get('days',{})
    except (OSError,ValueError):return {}


def merge_path(record,old):
    """LS 시간대 행은 최대 500개(30초)라 늦게 다시 조회하면 장 초반이 잘린다(10/22: 16:25 조회 저점 12:40 −1,657 →
    19:20 조회 저점 14:48 −1,555). 같은 날 먼저 본 저점·고점과 그 시각의 미결제 기록을 잃지 않게 합친다."""
    o,n=(old or {}).get('foreign_path'),record.get('foreign_path')
    if not o or not n:
        if o and not n:record['foreign_path']=o
        return
    low=o if o['low']<n['low'] else n;high=o if o['high']>n['high'] else n
    record['foreign_path']={'low':low['low'],'low_time':low['low_time'],'high':high['high'],'high_time':high['high_time']}
    old_marks=((old.get('futures') or {}).get('marks') or {})
    if old_marks and record.get('futures'):
        record['futures']['marks']={**old_marks,**(record['futures'].get('marks') or {})}


def save(record,directory=None):
    with SAVE_LOCK:
        return _save(record,directory)


def _save(record,directory=None):
    directory=Path(directory or ROOT/'data');days=load(directory)
    old=days.get(record['day'])
    if old and old.get('status')=='확정 추정' and record['status']!='확정 추정':return False  # 확정을 잠정으로 덮지 않는다
    if old and old.get('status')=='확정 추정' and old.get('qty')!=record.get('qty'):
        # 확정 추정 뒤에 값이 다시 바뀐 경우를 기록해 화면에 알린다.
        record['revised_from']={'qty':old.get('qty'),'last_change':old.get('last_change'),'asked_at':old.get('asked_at')}
    elif old and 'revised_from' in old and 'revised_from' not in record:
        record['revised_from']=old['revised_from']
    merge_path(record,old)
    # 같은 날의 현물·옵션·선물 시세 부가정보는 새 조회에 없으면 이전 것을 유지
    for k in ('spot','options','futures'):
        if k not in record and old and k in old:record[k]=old[k]
    if old and old.get('options') and record.get('options'):
        if str(record['options'].get('generated_at',''))<str(old['options'].get('generated_at','')):
            record['options']=old['options']
    days[record['day']]=record
    atomic_json(directory/FILE,{'days':dict(sorted(days.items())),'updated_at':record['asked_at']})
    return True


LATE_SPOT='15:15'


def spot_observed(spot_row):
    """현물 순매수 값이 실제로 관측된 시각. 일별 행은 항목별 observed_at, REST 마감 후 조회는 asof."""
    obs=spot_row.get('observed_at')
    if isinstance(obs,dict):obs=obs.get('foreign_spot_amt')
    return str(obs or spot_row.get('asof') or '')[:19]


def spot_is_late(spot,day):
    """헤지 해석에 쓸 만큼 마감에 가까운 값인가: 그날 15:15 이후 관측이거나 장 마감 후 조회."""
    obs=spot.get('observed_at','')
    return obs[:10]==day and obs[11:16]>=LATE_SPOT or str(spot.get('source','')).startswith('REST 마감 후')


FUT_CLOSE='15:46'


def tick_at(ticks,day,hms_text):
    """장중 분 기록 중 그 시각 이전 마지막 것의 선물 가격·미결제(2분 넘게 떨어지면 없음)."""
    target='%sT%s'%(day,hms_text)
    before=[t for t in ticks or [] if t['at'][:10]==day and t['at'][:19]<=target]
    if not before:return None
    t=before[-1];v=t.get('values') or {}
    if (dt.datetime.fromisoformat(target)-dt.datetime.fromisoformat(t['at'][:19])).total_seconds()>120:return None
    oi,price=number((v.get('fut_oi') or {}).get('value')),number((v.get('fut_price') or {}).get('value'))
    return {'at':t['at'][:19],'oi':oi,'price':price} if oi is not None else None


def futures_close(directory,daily_row,day,ticks=None,times=()):
    """그날 근월 선물 마감 가격·전일 종가·시장 전체 미결제·미결제 전일 대비.

    선물 장은 15:45에 끝난다. 그 뒤 LS t2111 조회본(snapshot_futures.json)이 같은 날이면 그것을 쓰고,
    없으면 장중 마지막 기록(일별 행)을 쓴다. 근월물이 바뀐 날(롤오버)도 코드를 함께 남긴다.
    times(외국인 장중 저점·고점 시각)의 미결제·가격을 장중 기록에서 찾아 marks로 남긴다."""
    try:
        snap=json.loads((Path(directory)/'snapshot_futures.json').read_text(encoding='utf-8'))['snapshot']
    except (OSError,ValueError,KeyError):
        snap=None
    if snap and str(snap.get('received_at',''))[:10]==day and str(snap['received_at'])[11:16]>=FUT_CLOSE:
        v=snap.get('values') or {}
        values={'price':v.get('fut_price'),'prev_close':v.get('fut_prev_close'),'oi':v.get('fut_oi'),'oi_change':v.get('fut_oi_prev')}
        observed,source,code=str(snap['received_at'])[:19],'LS t2111 장 마감 후 조회',snap.get('code')
    elif daily_row:
        obs=daily_row.get('observed_at') or {}
        values={'price':daily_row.get('fut_price'),'prev_close':daily_row.get('fut_prev_close'),'oi':daily_row.get('fut_oi'),'oi_change':daily_row.get('fut_oi_prev')}
        observed,source,code=str(obs.get('fut_oi') or obs.get('fut_price') or '')[:19],'장중 마지막 기록',daily_row.get('fut_code')
        if observed[:10]!=day:return None
    else:
        return None
    values={k:number(x) for k,x in values.items()}
    if values['price'] is None and values['oi'] is None:return None
    pct=(values['price']/values['prev_close']-1)*100 if values['price'] and values['prev_close'] else None
    marks={t:m for t in times if t for m in [tick_at(ticks,day,t)] if m}
    return {**values,'price_pct':pct,'code':code,'observed_at':observed,'source':source,'marks':marks}


def add_extras(record,spot_row,option_view,futures=None):
    """같은 날의 현물·옵션 해석·선물 시세를 붙인다. 날짜가 다르면 붙이지 않는다. 현물은 관측 시각·출처를 함께 저장."""
    if futures:
        record['futures']=futures
    if spot_row:
        spot={k:spot_row.get(k) for k in ('k200','foreign_spot_amt','inst_spot_amt','nonarb_amt','basis','basis_residual')}
        spot={k:v for k,v in spot.items() if number(v) is not None}
        obs=spot_observed(spot_row)
        if spot and (obs[:10]==record['day'] or str(spot_row.get('source','')).startswith('REST 마감 후')):
            source=spot_row.get('source') or '장중 마지막 기록'
            record['spot']={**spot,'observed_at':obs,'source':source,
                            'note':'마감 확정치 아님. 영웅문 0780 확정치와 소폭 다를 수 있음'}
    if option_view and option_view.get('status')=='received' and str(option_view.get('data_day') or option_view.get('generated_at',''))[:10]==record['day']:
        record['options']={'generated_at':option_view['generated_at'],
            'investors':[{'label':g['label'],'side':g['explain'].get('side'),'text':g['explain']['text'],
                          'kiwoom':(g['delta'].get('kiwoom') or {}).get('total'),'bs':(g['delta'].get('bs') or {}).get('total')}
                         for g in option_view.get('flows',[]) if g.get('available')]
                        # LS 옵션 투자자 합계는 콜·풋 구분 확인 전이라 방향(side) 없이 숫자 줄만 붙인다.
                        +[{'label':'LS '+r['key'],'side':None,'text':r['text'],'kiwoom':None,'bs':None}
                          for r in (option_view.get('ls_totals') or {}).get('rows',[])],
            'structure_text':option_view.get('structure_text',''),
            'methodology':deepcopy(option_view.get('methodology')),
            'quotes_at':deepcopy(option_view.get('quotes_at'))}
    return record


def refresh_pending_options(directory,option_view,at,cfg):
    """당일 확정 전 부가정보만 갱신. 확정·과거 기록은 화면 재계산으로 덮지 않는다."""
    day=at.date().isoformat()
    with SAVE_LOCK:
        record=load(directory).get(day)
        if not record or is_final(record,at,cfg):return False
        candidate=add_extras(deepcopy(record),None,option_view)
        if candidate.get('options')==record.get('options'):return False
        if str((candidate.get('options') or {}).get('generated_at',''))<str((record.get('options') or {}).get('generated_at','')):return False
        candidate['options_updated_at']=at.isoformat()
        return save(candidate,directory)


def method_note(options):
    method=(options or {}).get('methodology') or {}
    band=method.get('near_band_pct')
    return ('저장 당시 기준 · 지수 ±%g%% · %s'%(band,method.get('id',''))
            if band is not None else '저장 당시 기준 · 계산 방식 미기록(범위 추정 안 함)')


def sgn(v,digits=0):
    if v is None:return '—'
    return format(v,'+,.%df'%digits).replace('-','−')


def day_label(day):
    d=dt.date.fromisoformat(day);return '%d/%d(%s)'%(d.month,d.day,WEEKDAY[d.weekday()])


def side_word(v):
    return '순매수' if v>0 else '순매도' if v<0 else '순매매 없음'


MINI_KEYS={'17':'foreign','18':'inst','08':'individual','01':'financial'}
MINI_RATIO=0.2  # 미니 승수 5만 원 / 일반 25만 원


def mini_for(directory,day):
    """같은 날 한투 기록 중 미니선물 값이 있는 마지막 기록(계약·억원). 16:15 이후 기록이면 final.
    LS t1602 선물은 일반선물만이라 미니를 따로 붙인다. 한투 기록이 없는 날(키 미설정·수집기 꺼짐)은 None."""
    import deriv_today as D
    rows=[r for r in D.load_day(directory or ROOT/'data',day) if (r.get('markets') or {}).get('mini')]
    if not rows:return None
    r=rows[-1];m=r['markets']['mini']
    return {'at':r['at'][:19],'final':r['at'][11:16]>=D.FINAL_AFTER,'source':'한투 '+D.INVESTOR_TR,
            'qty':{c:(m.get(k) or {}).get('qty') for c,k in MINI_KEYS.items()},
            'amt':{c:(m.get(k) or {}).get('amt') for c,k in MINI_KEYS.items()}}


def with_mini(record,code):
    """일반선물 + 미니 × 0.2(계약), 일반 금액 + 미니 금액(억원). 미니 값이 없으면 None(0으로 채우지 않는다)."""
    mini=record.get('mini') or {}
    q,mq=record['qty'].get(code),(mini.get('qty') or {}).get(code)
    if q is None or mq is None:return None
    a,ma=record['amt'].get(code),(mini.get('amt') or {}).get(code)
    return {'qty':round(q+MINI_RATIO*mq,1),'amt':round(a+ma) if a is not None and ma is not None else None,'mini_qty':mq}


def text_for(record,cfg=None):
    q,a=record['qty'],record['amt']
    parts=[]
    for code in ('17','18','08'):
        if code in q:
            parts.append('%s %s계약%s %s'%(NAMES[code],sgn(q[code]),'(%s억)'%sgn(a[code]) if code in a else '',side_word(q[code]) if code!='08' else ''))
    text='%s 마감 선물: %s'%(day_label(record['day']),' · '.join(p.strip() for p in parts))
    eq={c:e for c in ('17','18') for e in [with_mini(record,c)] if e}
    if eq:
        # 2026-10-29: 일반 +86 · 미니 −2,745 → 환산 −463계약. 일반선물만 보면 현물과 '반대'로 잘못 읽었다.
        text+=' · 미니 포함(미니 0.2배 환산) '+' · '.join('%s %s계약%s %s'%(NAMES[c],sgn(e['qty']),'(%s억)'%sgn(e['amt']) if e['amt'] is not None else '',side_word(e['qty']))
                                               for c,e in eq.items())
    elif not record.get('mini'):
        text+=' · 미니선물 기록 없음(일반선물만)'
    spot=record.get('spot') or {}
    fs=number(spot.get('foreign_spot_amt'))
    fa=eq['17']['amt'] if '17' in eq and eq['17']['amt'] is not None else a.get('17')
    if fs is not None and fa is not None:
        late=spot_is_late(spot,record['day'])
        when='장 마감 후 조회' if str(spot.get('source','')).startswith('REST 마감 후') else '%s 기록'%spot.get('observed_at','')[11:16]
        if late:
            basis='미니 포함 선물' if '17' in eq else '일반선물'
            hedge=''
            if min(abs(fs),abs(fa))>=100:
                hedge=(' → 현물·%s이 반대 방향(헤지·차익일 수 있으나 같은 계좌인지는 확인 불가)'%basis if fs*fa<0
                       else ' → 현물·%s 모두 %s(같은 방향)'%(basis,side_word(fs)))
            text+=' · 같은 날 외국인 현물 %s억(%s)%s'%(sgn(fs),when,hedge)
        else:
            # 마감 한참 전 값으로는 현물·선물 관계를 해석하지 않는다.
            text+=' · 외국인 현물 %s억은 %s이라 마감 수급과 다를 수 있어 비교하지 않습니다'%(sgn(fs),when)
    fut=record.get('futures')
    if fut:
        quad=futures_quadrant(fut,cfg)
        text+=' · 선물 %s(%s%%) · 미결제 %s계약 → %s'%(format(fut['price'],',.2f') if fut.get('price') is not None else '—',
                                               sgn(fut.get('price_pct'),2),sgn(fut.get('oi_change')),quad['label'])
    return text


def futures_quadrant(fut,cfg=None):
    r=price_oi_rules(cfg or {})
    return price_oi_quadrant((fut or {}).get('price_pct'),(fut or {}).get('oi_change'),r['price_pct']['day'],r['oi']['day'])


def reading_for(record,cfg=None):
    """마감 기준 사실/추론/한계. 현물은 마감 무렵 값일 때만 헤지 해석에 쓴다."""
    fut=record.get('futures')
    if not fut:return None
    spot=record.get('spot') or {}
    fs=number(spot.get('foreign_spot_amt')) if spot and spot_is_late(spot,record['day']) else None
    ef,ei=with_mini(record,'17'),with_mini(record,'18')
    out=reading('day',ef['qty'] if ef else record['qty'].get('17'),fut.get('oi_change'),fut.get('price_pct'),fs,
                ei['qty'] if ei else record['qty'].get('18'),cfg,'그날 하루')
    for e,code,word in ((ef,'17','외국인 선물'),(ei,'18','기관계 선물')):
        i=next((i for i,f in enumerate(out['facts']) if f.startswith(word)),None)
        if e and i is not None:
            out['facts'][i]='%s(일반 %s · 미니 %s → 일반 환산) %s계약 %s'%(word,sgn(record['qty'][code]),sgn(e['mini_qty']),sgn(e['qty']),side_word(e['qty']))
    if not ef:
        out['limits'].append('미니선물 기록(한투)이 없어 일반선물만으로 판단했습니다.')
    if record['status']!='확정 추정':
        out['limits'].insert(0,'선물 투자자 값이 아직 %s이라 해석이 바뀔 수 있습니다.'%record['status'])
    if fut.get('source')=='장중 마지막 기록':
        out['limits'].insert(0,'선물 가격·미결제는 %s 장중 마지막 기록이며 마감 후 조회값이 아닙니다.'%fut.get('observed_at','')[11:16])
    if spot and fs is None and number(spot.get('foreign_spot_amt')) is not None:
        out['limits'].append('외국인 현물은 마감 한참 전 값이라 해석에 쓰지 않았습니다.')
    return out


def swing_reading(record,cfg=None):
    """당일 순매매만으로는 안 보이는 장중 반전(예: 7/24 −13,460 → −759, +12,701 되사들임)을 그 구간의 미결제·가격과 엮는다.
    반전 폭이 기준 이상이고, 당일 순매매와 방향이 다르거나 2배 이상 클 때만 만든다."""
    path,fut,final=record.get('foreign_path') or {},record.get('futures') or {},record['qty'].get('17')
    if final is None or not path:return None
    th=price_oi_rules(cfg or {})['foreign']['day'];best=None
    for kind in ('low','high'):
        v,t=path.get(kind),path.get(kind+'_time')
        if v is None or not t:continue
        move=final-v
        if abs(move)<th or (move*final>0 and abs(move)<2*abs(final)):continue
        if not best or abs(move)>abs(best[0]):best=(move,v,t)
    if not best:return None
    move,v,t=best;mark=(fut.get('marks') or {}).get(t)
    oi=fut['oi']-mark['oi'] if mark and fut.get('oi') is not None else None
    pct=(fut['price']/mark['price']-1)*100 if mark and mark.get('price') and fut.get('price') else None
    spot=record.get('spot') or {}
    fs=number(spot.get('foreign_spot_amt')) if spot and spot_is_late(spot,record['day']) else None
    out=reading('day',move,oi,pct,fs,None,cfg,'%s 이후 구간의'%t[:5])
    # 누적 순매수의 증감일 뿐, 그 사이 신규 매도인지 기존 롱 청산인지는 알 수 없다(2026-10-23 검토).
    out['title']='장중 반전: 외국인 누적 %s계약(%s) → 마감 %s계약, 누적 순매수 %s계약 %s'%(sgn(v),t[:5],sgn(final),format(abs(round(move)),','),'증가' if move>0 else '감소')
    if mark:
        out['facts'].insert(0,'구간: %s(미결제 %s) → 마감(미결제 %s)'%(t[:5],format(mark['oi'],',.0f'),format(fut['oi'],',.0f')))
    else:
        out['limits'].insert(0,'%s 시각의 미결제 기록이 없어 이 구간의 신규·청산을 구분하지 못했습니다(장중 기록은 실행 중일 때만 쌓임).'%t[:5])
    if fs is not None:
        out['limits'].insert(0,'외국인 현물은 하루 전체 값이고, 선물은 %s 이후 구간 값입니다.'%t[:5])
    return out


def notes_for(record):
    notes=[];st=record['status'];s35=(record.get('at_1535') or {}).get('qty') or {}
    if st!='확정 추정':
        notes.append('아직 %s입니다(마지막 변화 %s). 확정은 보통 16시 이후이며, 그 전 값은 크게 바뀔 수 있습니다.'%(st,record['last_change'][:5]))
    f,f35=record['qty'].get('17'),s35.get('17')
    if f is not None and f35 is not None and abs(f-f35)>=500:
        notes.append('외국인 선물 순매수가 장중 마지막(15:35) %s계약에서 마감 후 %s계약으로 바뀌었습니다. 장중에 본 값은 잠정치였습니다.'%(sgn(f35),sgn(f)))
    rev=record.get('revised_from') or {}
    r17=(rev.get('qty') or {}).get('17')
    if r17 is not None and r17!=f:
        notes.append('확정 추정으로 저장한 뒤(%s 변화분 %s계약) 값이 다시 바뀌어 %s계약으로 고쳤습니다.'%((rev.get('last_change') or '')[:5],sgn(r17),sgn(f)))
    return notes


def view(directory=None,n=10,cfg=None,current_options=None):
    days=load(directory)
    if not days:return {'status':'empty','note':'마감 요약이 아직 없습니다. 장 마감 후(또는 휴장일) 실시간실행을 켜 두면 LS에서 자동으로 받아 저장합니다.'}
    ordered=[days[d] for d in sorted(days,reverse=True)][:n]
    ordered=[r if r.get('mini') else {**r,'mini':mini_for(directory,r['day'])} for r in ordered]
    rows=[]
    for r in ordered:
        q,a=r['qty'],r['amt'];opt=next((g for g in (r.get('options') or {}).get('investors',[]) if g['label']=='외국인'),None)
        rows.append({'day':r['day'],'label':day_label(r['day']),'status':r['status'],'option_method_note':method_note(r.get('options')),
          'foreign_qty':q.get('17'),'foreign_amt':a.get('17'),'foreign_eq':with_mini(r,'17'),'inst_qty':q.get('18'),'inst_amt':a.get('18'),
            'individual_qty':q.get('08'),'foreign_spot':(r.get('spot') or {}).get('foreign_spot_amt'),
            'spot_note':('' if not r.get('spot') else '마감 후 조회' if str(r['spot'].get('source','')).startswith('REST 마감 후')
                         else r['spot'].get('observed_at','')[11:16]+' 기록'+('' if spot_is_late(r['spot'],r['day']) else ' · 이른 값')),
            'k200':(r.get('spot') or {}).get('k200'),'option_kiwoom':opt and opt['kiwoom'],'option_bs':opt and opt['bs'],
            'fut_price':(r.get('futures') or {}).get('price'),'fut_pct':(r.get('futures') or {}).get('price_pct'),
            'oi':(r.get('futures') or {}).get('oi'),'oi_change':(r.get('futures') or {}).get('oi_change'),
            'quadrant':futures_quadrant(r['futures'],cfg)['label'] if r.get('futures') else None})
    try:
        import investor_history as H
        hist=(H.load(directory or ROOT/'data').get('days') or {})
    except Exception:
        hist={}
    for row in rows:
        q=((hist.get(row['day']) or {}).get('fut') or {}).get('qty') or {}
        if q.get('foreign') is None:
            row['cross']=None;continue
        same=all(q.get(k)==row[f] for k,f in (('foreign','foreign_qty'),('inst','inst_qty'),('individual','individual_qty')) if row.get(f) is not None)
        row['cross']={'match':same,'foreign':q.get('foreign'),'inst':q.get('inst'),'individual':q.get('individual')}
    latest=ordered[0]
    cross=rows[0].get('cross') if rows else None
    notes=notes_for(latest)
    if cross and not cross['match']:
        notes.insert(0,'LS 일별 확정 이력(t1665)과 다릅니다: 외국인 %s · 기관계 %s · 개인 %s계약. 누적·일별 기록은 t1665 값을 씁니다.'%(
            sgn(cross['foreign']),sgn(cross['inst']),sgn(cross['individual'])))
    recalculated=None
    if current_options and current_options.get('status')=='received' and current_options.get('data_day')==latest['day']:
        recalculated={'label':'현재 기준 재계산 · 저장본 보존',
                      'text':current_options.get('structure_text',''),
                      'methodology':current_options.get('methodology'),
                      'quotes_at':current_options.get('quotes_at')}
    return {'status':'received','cross':cross,'latest':{**latest,'text':text_for(latest,cfg),'notes':notes,'reading':reading_for(latest,cfg),'swing_reading':swing_reading(latest,cfg),
            'option_method_note':method_note(latest.get('options')),'recalculated_options':recalculated,
            'option_lines':[relabel(g['text']) for g in (latest.get('options') or {}).get('investors',[])],
            'structure_text':(latest.get('options') or {}).get('structure_text','')},'rows':rows}


FLOW_CODES={'외국인':'17','기관계':'18','금융투자':'01','개인':'08'}


def flow_close(directory=None,cfg=None,at=None):
    """장 마감 뒤 '외국인·기관 흐름 비교' 표에 쓸 값(2026-10-29 검토: 마감 뒤에도 15:50 장중 잠정치가 남았다).
    선물은 마감 요약(LS t1602 마감 후 시계열, 미니 환산 포함), 현물은 LS t1601 통합(KRX+NXT) 장 마감 후 조회.
    같은 거래일 값만 쓰고, 장중이면 None."""
    from common import now_kst,stamp
    import investor_board as ib
    directory=Path(directory or ROOT/'data');at=stamp(at or now_kst())
    day=target_day(at,cfg)
    if day is None:return None
    day=day.isoformat();rec=load(directory).get(day)
    if rec and not rec.get('mini'):rec={**rec,'mini':mini_for(directory,day)}
    try:board=json.loads((directory/ib.FILE).read_text(encoding='utf-8'))
    except (OSError,ValueError):board=None
    stock=None
    if board and board.get('generated_at','')[:10]==day and board['generated_at'][11:16]>=SESSION_END:
        stock={r['label']:r['net'] for m in board.get('markets',[]) if m.get('key')=='stock' for r in m['rows']}
    if not rec and not stock:return None
    rows={}
    for name,code in FLOW_CODES.items():
        e=with_mini(rec,code) if rec else None
        rows[name]={'fut':rec['qty'].get(code) if rec else None,'fut_amt':rec['amt'].get(code) if rec else None,
                    'fut_eq':e and e['qty'],'fut_eq_amt':e and e['amt'],'spot':(stock or {}).get(name)}
    return {'day':day,'rows':rows,
            'fut':{'status':rec['status'],'last_change':rec['last_change'][:5],'source':'LS t1602 마감 후 시계열',
                   'mini':bool(rec.get('mini'))} if rec else None,
            'spot':{'at':board['generated_at'],'source':'LS t1601 통합(KRX+NXT) 코스피 · 장 마감 후 조회'} if stock else None}
