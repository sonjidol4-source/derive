import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from common import config,stamp
from engine import Engine
from importers import flow_packet
from investor_probe import RECORD
from option_analytics import bs_delta,build,combined_text,decompose,explain,max_pain,oi_structure

CFG=config()
DAY='2026-10-15T10:00:00+09:00'


def quote(code,cp,strike,oi,delta_raw,iv=50):
    return {'code':code,'cp':cp,'strike':strike,'month':'202611','oi':oi,'iv':iv,'delta_raw':delta_raw}


class Fixture:
    def __init__(self,d,call_delta=50,put_delta=-50,quoted_at=DAY):
        self.d=Path(d)
        for kind,row,complete in (('calls',quote('B1','C',860,100,call_delta),True),('puts',quote('C1','P',860,100,put_delta),True)):
            snap={'received_at':quoted_at,'complete':complete,'listed_count':1,'rows':[row]}
            (self.d/('snapshot_'+kind+'.json')).write_text(json.dumps({'snapshot':snap}),encoding='utf-8')
        fut={'received_at':quoted_at,'values':{'k200':860}}
        (self.d/'snapshot_futures.json').write_text(json.dumps({'snapshot':fut}),encoding='utf-8')

    def investors(self,at,call_qty='-400',put_qty='+100',rows=None,response=True):
        rows=rows if rows is not None else [{'종목코드':'B1','풋_종목코드':'C1','행사가':'860','투자자별순매수수량':call_qty,'풋_투자자별순매수수량':put_qty}]
        result={'investor':'foreign','label':'외국인','scope':'daily_net_trades','received_at':at,'message':'',
                'response':{RECORD:rows} if response else None}
        (self.d/'investor_options.json').write_text(json.dumps({'generated_at':at,'results':[result]}),encoding='utf-8')


class OptionAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.f=Fixture(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def run_build(self,at=DAY,fut=None,spot=860):
        return build(self.f.d,spot,'test',fut or {},CFG,at)

    def test_bs_delta_put_call_parity(self):
        c=bs_delta('C',860,860,50,.08);p=bs_delta('P',860,860,50,.08)
        self.assertAlmostEqual(c-p,1,places=9);self.assertTrue(.5<c<.6)

    def test_max_pain_minimizes_holder_payout(self):
        calls=[{'strike':100,'oi':10},{'strike':110,'oi':50}];puts=[{'strike':90,'oi':40},{'strike':100,'oi':10}]
        self.assertEqual(max_pain(calls,puts),100)

    def test_near_walls_and_edge_flag(self):
        q={'C1':quote('C1','C',870,500,40),'C2':quote('C2','C',1200,5000,1),'P1':quote('P1','P',850,1500,-40),'P2':quote('P2','P',750,6000,-1)}
        st=oi_structure(q,860,CFG,DAY,complete=False)
        self.assertEqual(st['near_call_wall']['strike'],870);self.assertEqual(st['near_put_wall']['strike'],850)
        self.assertTrue(st['put_walls'][0]['edge']);self.assertIn('미수신',st['coverage'])

    def test_round_strike_just_outside_5pct_is_a_wall(self):
        # 2026-10-29: 지수 935.87, 1000(+6.85%) 미결제 6,951계약이 ±5% 범위에서 빠졌다.
        q={'C1':quote('C1','C',950,4268,40),'C2':quote('C2','C',1000,6951,10),'C3':quote('C3','C',1100,6956,1),
           'P1':quote('P1','P',900,2041,-40),'P2':quote('P2','P',875,3000,-10)}
        st=oi_structure(q,935.87,CFG,DAY)
        self.assertEqual(st['near_call_wall']['strike'],1000);self.assertEqual(st['near_put_wall']['strike'],875)
        self.assertIn(1000,[r['strike'] for r in st['rows']])  # 차트에도 보인다

    def test_live_delta_signs_and_combined(self):
        self.f.investors(DAY)
        a=self.run_build(fut={'foreign_fut_qty':{'value':-1000,'at':'2026-10-15T10:03:00+09:00'}})
        g=a['flows'][0]
        self.assertEqual(a['mode'],'live')
        # 콜 -400 × 0.5 = -200, 풋 +100 × -0.5 = -50 (키움 델타 % → 소수)
        self.assertEqual(g['delta']['kiwoom']['total'],-250);self.assertEqual(g['delta']['kiwoom']['put_buy'],-50)
        self.assertEqual(g['combined']['kiwoom'],-1250);self.assertIn('매도 상당',g['explain']['combined_text'])
        self.assertEqual(g['explain']['side'],-1);self.assertIn('오늘',g['explain']['text'])

    def test_prior_day_options_are_not_today_and_not_combined(self):
        # 검토 1번: 10/15 파일을 10/16 장중에 쓰면 안 된다.
        self.f.investors(DAY)
        a=self.run_build(at='2026-10-16T10:00:00+09:00',fut={'foreign_fut_qty':{'value':1000,'at':'2026-10-16T10:00:00+09:00'}})
        g=a['flows'][0]
        self.assertEqual(a['mode'],'stale');self.assertIsNone(g['explain']['side']);self.assertIsNone(g['combined'])
        self.assertIn('보류',g['explain']['text'])

    def test_first_poll_after_open_is_prior_day_not_today(self):
        # 검토 보완 4: 08:50 조회는 직전 거래일 일자로 받으므로 '오늘'이라고 쓰지 않는다.
        at='2026-10-16T08:50:00+09:00';Fixture(self.temp.name,quoted_at=at);self.f.investors(at)
        report=json.loads((self.f.d/'investor_options.json').read_text(encoding='utf-8'));report['data_day']='2026-10-15'
        (self.f.d/'investor_options.json').write_text(json.dumps(report),encoding='utf-8')
        a=self.run_build(at=at,fut={'foreign_fut_qty':{'value':-1000,'at':at}})
        g=a['flows'][0]
        self.assertEqual(a['mode'],'prior');self.assertNotIn('오늘',g['explain']['text']);self.assertIn('10/15',g['explain']['text'])
        self.assertIsNone(g['combined'])

    def test_after_close_is_archive_with_date(self):
        self.f.investors(DAY)
        a=self.run_build(at='2026-10-15T20:00:00+09:00')
        self.assertEqual(a['mode'],'archive');self.assertIn('10/15 10:00 기준 그날',a['flows'][0]['explain']['text'])

    def test_old_futures_input_is_not_combined(self):
        self.f.investors(DAY)
        g=self.run_build(fut={'foreign_fut_qty':{'value':-1000,'at':'2026-10-15T09:40:00+09:00'}})['flows'][0]
        self.assertIsNone(g['combined'])

    def test_quotes_from_other_round_block_calculation(self):
        Fixture(self.temp.name,quoted_at='2026-10-15T09:30:00+09:00');self.f.investors(DAY)
        # 다른 회차 시세로는 델타 환산을 하지 않는다. OI 구조(시세만)는 계속 보여 준다(LS 전환 후).
        A=self.run_build()
        self.assertEqual(A['flows'],[]);self.assertIsNotNone(A['structure'])

    def test_empty_missing_quantity_and_missing_delta_are_not_zero(self):
        # 검토 2번 세 사례
        self.f.investors(DAY,rows=[])
        g=self.run_build()['flows'][0]
        self.assertFalse(g['available']);self.assertIsNone(g['delta']['kiwoom']);self.assertIn('데이터 없음',g['explain']['text'])
        self.f.investors(DAY,call_qty='',put_qty='')
        g=self.run_build()['flows'][0]
        self.assertFalse(g['available']);self.assertIn('데이터 없음',g['explain']['text'])
        Fixture(self.temp.name,call_delta=None);self.f.investors(DAY,call_qty='1000',put_qty='0')
        g=self.run_build()['flows'][0]
        self.assertTrue(g['partial']);self.assertEqual(g['missing_delta'],1);self.assertIsNone(g['explain']['side'])
        self.assertIn('보류',g['explain']['text'])

    def test_real_zero_is_valid(self):
        self.f.investors(DAY,call_qty='0',put_qty='0')
        g=self.run_build()['flows'][0]
        self.assertTrue(g['available']);self.assertEqual(g['delta']['kiwoom']['total'],0);self.assertEqual(g['explain']['side'],0)

    def test_combined_opposite_signs_are_not_called_buy(self):
        # 검토 4번
        g={'combined':{'kiwoom':54.2,'bs':-54.2}}
        text=combined_text(g,{'value':254,'at':DAY},50)
        self.assertIn('엇갈림',text);self.assertNotIn('매수 상당',text)

    def test_explain_requires_both_methods_to_agree(self):
        g={'label':'외국인','message':'','available':True,'partial':False,'missing_qty':0,'missing_delta':0,'unmatched':0,
           'delta':{'kiwoom':{'total':-300,'call_buy':0,'call_sell':-300,'put_buy':0,'put_sell':0},
                    'bs':{'total':20,'call_buy':0,'call_sell':20,'put_buy':0,'put_sell':0}}}
        self.assertIsNone(explain(g)['side'])
        g['delta']['bs']['total']=-200
        e=explain(g);self.assertEqual(e['side'],-1);self.assertIn('200~300계약 매도',e['text'])

    def test_change_separates_trading_from_delta_revaluation(self):
        # 검토 5번: 수량 그대로, 델타만 0.5→0.7이면 매매분 0, 재평가 +20.
        base={'totals':{'kiwoom':50,'bs':50},'qty':{'B1':100},'delta':{'B1':{'kiwoom':.5,'bs':.5}}}
        now={'totals':{'kiwoom':70,'bs':70},'qty':{'B1':100},'delta':{'B1':{'kiwoom':.7,'bs':.7}}}
        c=decompose(now,base)['kiwoom']
        self.assertEqual((c['total'],c['trade'],c['revalue']),(20,0,20))
        now={'totals':{'kiwoom':84,'bs':84},'qty':{'B1':120},'delta':{'B1':{'kiwoom':.7,'bs':.7}}}
        c=decompose(now,base)['kiwoom'];self.assertEqual((c['trade'],c['revalue']),(14,20))

    def test_history_change_between_rounds(self):
        self.f.investors(DAY,call_qty='-400')
        self.run_build()
        later='2026-10-15T10:05:00+09:00';self.f.investors(later,call_qty='-200')
        Fixture(self.temp.name,quoted_at=later)
        b=self.run_build(at=later);self.run_build(at=later)
        c=b['flows'][0]['change_prev']['kiwoom']
        self.assertEqual((c['total'],c['trade'],c['revalue']),(100,100,0))
        self.assertEqual(len((self.f.d/'option_flow_history.jsonl').read_text(encoding='utf-8').splitlines()),2)

    def test_missing_files_are_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(build(d,860,'',{},CFG)['status'],'unavailable')


class ManualAndObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.e=Engine(CFG,self.temp.name)
    def tearDown(self):self.e.store.close();self.temp.cleanup()

    def test_manual_inputs_never_make_five_minute_changes(self):
        # 검토 3번: 10:00 100, 10:10 500 → 5분 변화 없음, 직전 입력 대비 +400/10분.
        t0=stamp(DAY)
        for minute in range(0,15):
            at=t0+dt.timedelta(minutes=minute)
            if minute==0:self.e.ingest(flow_packet('asof,foreign_fut_qty\n'+at.isoformat()+',100','영웅문 0780 · 수동 입력'),clock=at)
            if minute==10:self.e.ingest(flow_packet('asof,foreign_fut_qty\n'+at.isoformat()+',500','영웅문 0780 · 수동 입력'),clock=at)
            self.e.ingest({'asof':at.isoformat(),'source':'OCX 선물시세','values':{'k200':860+minute*.1}},clock=at)
            s=self.e.compute(at)
            self.assertIsNone(s['metrics']['foreign_fut_qty']['m5'])
        row=[r for r in s['flows'] if r['investor']=='외국인'][0]
        self.assertEqual(row['fut_manual_change'],{'value':400,'minutes':10})
        self.assertIsNone(row['fut5'])

    def test_observation_is_recorded_once_per_cooldown_with_price(self):
        at=stamp(DAY)
        self.e.ingest({'asof':at.isoformat(),'source':'OCX 선물시세','values':{'k200':860.5}},clock=at)
        self.assertTrue(self.e.observe('옵션 외국인 하락 쪽',-1,at,['f'],{'source':'option_analytics'}))
        self.assertFalse(self.e.observe('옵션 외국인 하락 쪽',-1,at+dt.timedelta(minutes=5),['f'],{}))
        ev=self.e.store.events(10)[0];self.assertEqual((ev['kind'],ev['price'],ev['source']),('옵션 외국인 하락 쪽',860.5,'option_analytics'))
        self.assertFalse(self.e.observe('x',1,stamp('2026-10-15T20:00:00+09:00'),[],{}))


if __name__=='__main__':unittest.main()
