"""Startup quote responses are reference snapshots, never invented live ticks."""
from common import number


def futures_snapshot(result, code, received_at):
    main=next((r for r in result.get('선옵현재가정보',[]) if number(r.get('현재가')) not in (None,0)),None)
    if main is None:raise ValueError('선물 현재가 응답이 비었습니다')
    values={}
    for target,field in [('fut_price','현재가'),('fut_oi','미결제약정'),('fut_oi_prev','미결제약정전일대비'),('basis','시장베이시스'),('basis_theory','이론베이시스')]:
        value=number(main.get(field))
        if value is not None:values[target]=abs(value) if target=='fut_price' else value
    ticks=result.get('선옵시간대별체결',[])
    reference=next((abs(number(r['코스피200'])) for r in ticks if number(r.get('코스피200')) not in (None,0)),None)
    if reference is not None:values['k200']=reference
    return {'kind':'futures','code':code,'received_at':received_at,'market_asof':None,
            'values':values,'source':'Open API+ opt50001','note':'조회 응답의 마지막 시세입니다. 수신 시각은 체결 시각이 아니며 장중 신호에 사용하지 않습니다.'}


def options_snapshot(result, cp, month, received_at):
    record='콜종목결제월별시세' if cp=='C' else '풋종목결제월별시세'
    rows=[];seen=set()
    for row in result.get(record,[]):
        code=str(row.get('종목코드','')).strip()
        strike=number(row.get('행사가'))
        # 연속 조회 쪽 사이의 중복 행은 한 번만 반영.
        if not code or strike is None or code in seen:continue
        seen.add(code)
        item={'code':code,'strike':strike,'cp':cp,'month':month}
        for key,field in [('price','현재가'),('oi','미결제약정'),('iv','내재변동성'),('delta_raw','델타')]:
            value=number(row.get(field))
            if value is not None:item[key]=abs(value) if key=='price' else value
        rows.append(item)
    if not rows:raise ValueError('옵션 월물 시세 응답이 비었습니다')
    return {'kind':'calls' if cp=='C' else 'puts','month':month,'received_at':received_at,
            'market_asof':None,'rows':rows,'source':'Open API+ '+('opt50021' if cp=='C' else 'opt50022'),
            'note':'조회 응답의 마지막 시세이며 실시간 수신이나 투자자별 보유량을 뜻하지 않습니다. delta_raw는 서버 원문 수치로 보존하며 단위 변환·포지션 계산에 사용하지 않습니다.'}
