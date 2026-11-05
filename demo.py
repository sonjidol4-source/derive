"""결정론적 가상 시나리오. 네트워크·API 키를 사용하지 않음."""
import argparse
import datetime as dt
import math
import time
from pathlib import Path
from common import KST,config,atomic_json,ROOT
from engine import Engine

def populate(eng, date='2026-10-14', until=120):
    start=dt.datetime.fromisoformat(date+'T09:00:00+09:00')
    foreign,inst,nonarb,spot,oi=-2500,400,-1500,-1000,200000
    for i in range(until+1):
        at=start+dt.timedelta(minutes=i)
        # 미결제: 하락+증가(신규 매도) → 상승+감소(매도 청산) → 보합 → 하락+증가
        if i<22: slope=-100; cash=-70; price=500-.11*i; oi_step=30
        elif i<62: slope=145; cash=65; price=497.58+.12*(i-22); oi_step=-25
        elif i<82: slope=30; cash=-35; price=502.38-.02*(i-62); oi_step=3
        else: slope=-150; cash=-70;price=501.98-.12*(i-82); oi_step=35
        foreign+=slope;inst-=slope*.45;nonarb+=cash;spot+=cash*.7;oi+=oi_step
        # 10:28~10:36 외국인 데이터 중단. 값은 유지하되 수신시각을 갱신하지 않음.
        flows={'inst_fut_qty':inst,'nonarb_amt':nonarb,'program_amt':nonarb+100,
            'arb_amt':100,'foreign_spot_amt':spot,'inst_spot_amt':400-spot*.2,
            'financial_spot_amt':-200,'individual_spot_amt':-spot,'individual_fut_qty':-foreign-inst,
            'financial_fut_qty':inst*.5,'foreign_call_amt':35,'foreign_put_amt':-8}
        if not 88<=i<=96:flows['foreign_fut_qty']=foreign
        residual=-.4+.012*(i-22) if 22<=i<62 else -.1-.01*max(i-82,0)
        packet={'asof':at.isoformat(),'source':'가상 데모','fut_code':'DEMO202612','opt_month':'202611',
            'values':{**flows,'k200':price,'fut_price':price+1.2+residual,'basis':1.2+residual,
                'basis_theory':1.2,'fut_oi':oi,'fut_oi_prev':oi-199000,'fut_prev_close':500.6,'breadth':48+math.sin(i/15)*15},
            'stocks':[{'code':'005930','name':'삼성전자','price':85000+i*10,'change_pct':1.2,'foreign_amt':430,'inst_amt':-85},
                {'code':'000660','name':'SK하이닉스','price':200000+i*40,'change_pct':2.1,'foreign_amt':1200,'inst_amt':300}], 'options':[]}
        center=round(price/2.5)*2.5
        for d in range(-8,9):
            strike=center+2.5*d
            for cp in ('C','P'):
                packet['options'].append({'code':cp+str(strike),'cp':cp,'strike':strike,'month':'202611',
                    'oi':int(2000+7000*math.exp(-((d-(2 if cp=='C' else -2))/3)**2)),
                    'iv':25+abs(d)*.3,'delta':.5-d*.03 if cp=='C' else -.5-d*.03})
        eng.ingest(packet,at)
        snap=eng.compute(at)
        yield snap

def main():
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');p.add_argument('--minutes',type=int,default=120)
    args=p.parse_args();eng=Engine(config(),ROOT/'data_demo','demo')
    for table in ('ticks','events','daily','raw'):
        eng.store.db.execute('DELETE FROM '+table)
    eng.store.db.commit()
    for snap in populate(eng,until=args.minutes):
        atomic_json(ROOT/'data_demo'/'latest.json',snap)
        if not args.once:time.sleep(.5)
    print('가상 데모 완료. 신호 %d건'%len(eng.store.events()))
    eng.store.close()

if __name__=='__main__':main()
