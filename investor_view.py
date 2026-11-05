"""Display verified option response rows without treating them as holdings/futures."""
import json
from pathlib import Path
from common import ROOT,atomic_json,now_kst,number
from investor_probe import RECORD

OPTION_FILE='investor_options.json'
# opt50038 일자구분 1=일자(당일 순매매), 2=누적. 누적의 정확한 기준(상장 이후 순보유 추정 등)은 미검증.
SCOPES={'daily_net_trades':'당일 순매매','cumulative':'누적 순매수(일자구분=2)'}
NOTES={'daily_net_trades':'조회 범위의 옵션 당일 순매매입니다. 보유량·선물 수급·기관계 전체가 아닙니다.',
       'cumulative':'키움 일자구분=2 누적 값입니다. 2026-10-15 실조회에서 외국인 21개 행사가가 시장 미결제약정보다 커서 보유량으로 볼 수 없습니다. 누적 기준일은 미확인입니다.'}


def save_option_report(results,directory=None,data_day=None):
    directory=Path(directory or ROOT/'data')
    report={'generated_at':now_kst().isoformat(),'data_day':data_day,'trcode':'opt50038','results':results,
            'note':'수집기가 주기적으로 조회한 옵션 투자자 응답입니다. 실시간 틱이 아니며 자동 판정에 쓰지 않습니다.'}
    atomic_json(directory/OPTION_FILE,report)
    return report


def summarize(report,quotes):
    groups=[]
    for result in report.get('results',[]):
        scope=result.get('scope')
        if scope not in SCOPES:continue
        rows=[];unmatched=0
        for raw in (result.get('response') or {}).get(RECORD,[]):
            for cp,prefix in [('C',''),('P','풋_')]:
                code=str(raw.get(prefix+'종목코드','')).strip()
                if not code:continue
                quote=quotes.get(code)
                if not quote or quote.get('cp')!=cp:
                    unmatched+=1;continue
                strike=number(raw.get('행사가'))
                if strike is None or strike!=number(quote.get('strike')):
                    unmatched+=1;continue
                row={'code':code,'cp':cp,'strike':strike,'month':quote['month'],
                     'net_qty':number(raw.get(prefix+'투자자별순매수수량'))}
                if scope=='cumulative':
                    row.update(avg_price=number(raw.get(prefix+'매입단가')),pnl=number(raw.get(prefix+'평가손익')))
                rows.append(row)
        months=sorted({r['month'] for r in rows})
        def total(cp):
            selected=[r['net_qty'] for r in rows if r['cp']==cp]
            return sum(selected) if selected and all(q is not None for q in selected) else None
        groups.append({'investor':result['investor'],'label':result['label'],'scope':scope,'scope_label':SCOPES[scope],
            'received_at':result['received_at'],'source_rows':result.get('row_count',0),'message':result.get('message',''),
            'months':months,'rows':rows,'matched_options':len(rows),'unmatched_options':unmatched,
            'call_net_qty':total('C'),'put_net_qty':total('P'),
            'future_quantity_found':bool(result.get('future_quantity_found'))})
    scopes=[s for s in SCOPES if any(g['scope']==s for g in groups)]
    return {'status':'received' if any(g['rows'] for g in groups) else 'unavailable',
            'generated_at':report.get('generated_at'),'groups':groups,'is_live':False,'scopes':scopes,
            'notes':{s:NOTES[s] for s in scopes},
            'note':'계약수 합계는 델타를 반영하지 않습니다. 금융투자는 기관계 전체가 아니며 옵션 수량을 선물 수급으로 쓰지 않습니다.'}


def load_view(directory=None):
    directory=Path(directory or ROOT/'data')
    try:
        # 주기 수집본이 있으면 우선. 없으면 1회 점검 결과(당일 순매매만)를 표시.
        candidates=[directory/OPTION_FILE,directory/'investor_probe.json']
        reports=[json.loads(p.read_text(encoding='utf-8')) for p in candidates if p.exists()]
        if not reports:raise OSError('no report')
        report=max(reports,key=lambda r:r.get('generated_at',''))
        quotes={}
        for kind in ('calls','puts'):
            snapshot=json.loads((directory/('snapshot_'+kind+'.json')).read_text(encoding='utf-8'))['snapshot']
            # Only compare with quote snapshots from the same observation day.
            if snapshot['received_at'][:10]!=report['generated_at'][:10]:continue
            for row in snapshot['rows']:quotes[row['code']]=row
        return summarize(report,quotes)
    except (OSError,ValueError,KeyError,TypeError):
        return {'status':'unavailable','groups':[],'is_live':False,'note':'검증할 투자자 응답 또는 같은 날짜의 옵션 시세 파일이 없습니다.'}
