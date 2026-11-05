"""증권사 추정 포지션만 시나리오 계산. 당일 매매량을 보유량으로 사용하지 않음."""
import csv
import io
from common import number, stamp

def parse_positions(text):
    rows = list(csv.DictReader(io.StringIO(text.lstrip('\ufeff'))))
    if not rows or len(rows) > 2000:
        raise ValueError('포지션 CSV는 1~2,000행이어야 합니다')
    out = []
    groups = {}
    for r in rows:
        if r.get('scope') != 'broker_estimate':
            raise ValueError('보유량 추정 자료만 허용: scope=broker_estimate. 당일 순매매량은 사용할 수 없습니다')
        if r.get('product','KOSPI200') != 'KOSPI200':
            raise ValueError('정규 KOSPI200 선물·옵션만 지원합니다. 미니·주식선물은 계약 승수가 다릅니다')
        kind = r.get('kind')
        if kind not in ('F','C','P') or not r.get('investor') or not r.get('source'):
            raise ValueError('investor, kind(F/C/P), source가 필요합니다')
        at = stamp(r['asof']).isoformat()
        expiry = r.get('expiry','')
        stamp(expiry + 'T15:20:00+09:00')
        q, avg = number(r.get('net_qty')), number(r.get('avg_price'))
        strike = number(r.get('strike'))
        delta = number(r.get('delta'))
        if q is None or avg is None or avg < 0 or (kind != 'F' and strike is None):
            raise ValueError('수량, 평균단가, 옵션 행사가의 숫자 형식을 확인하세요')
        if delta is not None and (not -1 <= delta <= 1):
            raise ValueError('delta는 -1~1 범위입니다')
        if delta is not None and ((kind=='C' and delta<0) or (kind=='P' and delta>0)):
            raise ValueError('델타는 옵션 1계약의 값입니다. 콜은 양수, 풋은 음수이며 매도량은 net_qty의 부호로 입력합니다')
        key = (r['investor'], expiry)
        groups.setdefault(key, set()).add(at)
        out.append({'investor':r['investor'], 'kind':kind, 'expiry':expiry,
            'code':r.get('code',''), 'net_qty':q, 'avg_price':avg, 'strike':strike,
            'delta':1.0 if kind=='F' else delta, 'asof':at, 'source':r['source'], 'scope':'broker_estimate'})
    if any(len(s)>1 for s in groups.values()):
        raise ValueError('같은 투자자·만기의 포지션은 동일 asof 시점이어야 합니다')
    return out

def scenarios(rows, spot, at):
    if not rows or not spot:
        return {'groups':[], 'note':'증권사 추정 포지션 CSV를 연결하면 델타와 만기손익을 표시합니다. 전체 시장 미결제는 투자자별 보유량으로 사용할 수 없습니다.'}
    groups = []
    for investor, expiry in sorted({(r['investor'],r['expiry']) for r in rows}):
        items = [r for r in rows if (r['investor'],r['expiry'])==(investor,expiry)]
        pts = [round(spot * (1+x/100),2) for x in range(-8,9)]
        pnl = []
        for s in pts:
            result = 0
            for r in items:
                value = s if r['kind']=='F' else max(s-r['strike'],0) if r['kind']=='C' else max(r['strike']-s,0)
                result += r['net_qty']*(value-r['avg_price'])*250000/100000000
            pnl.append(round(result,3))
        delta_ok = all(r['delta'] is not None for r in items)
        dq = sum(r['net_qty']*r['delta'] for r in items) if delta_ok else None
        asof = stamp(items[0]['asof'])
        age = (stamp(at)-asof).total_seconds()/60
        groups.append({'investor':investor,'expiry':expiry,'asof':asof.isoformat(),'age_min':round(age,1),
            'stale':age>10 or age<0 or expiry<stamp(at).date().isoformat(),
            'delta_contracts':None if dq is None else round(dq,2),
            'delta_notional_eok':None if dq is None else round(dq*spot*.0025,2),
            'points':pts,'pnl_eok':pnl,'sources':sorted({r['source'] for r in items})})
    return {'groups':groups,'note':'증권사 추정 보유량·평균단가 기준 만기손익(억원). 같은 만기끼리 계산하며 비용은 미포함입니다. 델타 노출은 현재 지수 부근의 국소 추정이며 상승·하락 의도나 실제 보유잔고를 확정하지 않습니다.'}
