import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from common import config,stamp
from engine import Engine
from importers import flow_packet
from investor_probe import RECORD
from investor_view import load_view,save_option_report,summarize

QUOTES={'B1':{'cp':'C','strike':850,'month':'202611'},'C1':{'cp':'P','strike':850,'month':'202611'}}
ROW={'종목코드':'B1','풋_종목코드':'C1','행사가':'850','투자자별순매수수량':'-5','풋_투자자별순매수수량':'+3',
     '매입단가':'4.66','평가손익':'--12','풋_매입단가':'2.10','풋_평가손익':'+7'}


def result(scope,rows=(ROW,)):
    return {'investor':'foreign','label':'외국인','scope':scope,'received_at':'2026-10-15T10:00:00+09:00',
            'row_count':len(rows),'message':'','response':{RECORD:list(rows)}}


class OptionInvestorTests(unittest.TestCase):
    def test_daily_and_cumulative_are_separate_groups(self):
        view=summarize({'generated_at':'2026-10-15T10:00:00+09:00','results':[result('daily_net_trades'),result('cumulative')]},QUOTES)
        self.assertEqual(view['scopes'],['daily_net_trades','cumulative'])
        daily,cum=view['groups']
        self.assertNotIn('avg_price',daily['rows'][0])
        self.assertEqual((cum['rows'][0]['avg_price'],cum['rows'][0]['pnl']),(4.66,-12))
        self.assertEqual((cum['call_net_qty'],cum['put_net_qty']),(-5,3))

    def test_failed_request_keeps_unknown_not_zero(self):
        failed={**result('cumulative'),'response':None,'row_count':0,'message':'응답 시간 초과'}
        group=summarize({'generated_at':'x','results':[failed]},QUOTES)['groups'][0]
        self.assertIsNone(group['call_net_qty']);self.assertEqual(group['message'],'응답 시간 초과')

    def test_load_prefers_newer_periodic_report(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d)
            snap=lambda cp,code:{'snapshot':{'received_at':dt.date.today().isoformat()+'T09:00:00+09:00','rows':[{'code':code,**QUOTES[code]}]}}
            (d/'snapshot_calls.json').write_text(json.dumps(snap('C','B1')),encoding='utf-8')
            (d/'snapshot_puts.json').write_text(json.dumps(snap('P','C1')),encoding='utf-8')
            (d/'investor_probe.json').write_text(json.dumps({'generated_at':'2000-01-01T00:00:00+09:00','results':[]}),encoding='utf-8')
            save_option_report([result('cumulative')],d)
            view=load_view(d)
            self.assertEqual(view['status'],'received');self.assertEqual(view['scopes'],['cumulative'])


class ManualFuturesFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.e=Engine(config(),self.temp.name)
        self.at=stamp(dt.date.today().isoformat()+'T10:00:00+09:00')
    def tearDown(self):self.e.store.close();self.temp.cleanup()

    def test_manual_0780_value_stays_fresh_for_manual_ttl_only(self):
        text='asof,foreign_fut_qty,inst_fut_qty\n'+self.at.isoformat()+',-1234,560'
        self.assertTrue(self.e.ingest(flow_packet(text,'영웅문 0780 · 수동 입력'),clock=self.at))
        self.assertTrue(self.e.measurement('foreign_fut_qty',self.at+dt.timedelta(minutes=10))['fresh'])
        self.assertFalse(self.e.measurement('foreign_fut_qty',self.at+dt.timedelta(minutes=16))['fresh'])
        self.assertEqual(self.e.measurement('inst_fut_qty',self.at)['value'],560)

    def test_other_futures_code_is_rejected(self):
        with self.assertRaises(ValueError):flow_packet('asof,fut_code,foreign_fut_qty\n'+self.at.isoformat()+',A016D000,5',current_fut='A016C000')
        self.assertEqual(flow_packet('asof,fut_code,foreign_fut_qty\n'+self.at.isoformat()+',,5',current_fut='A016C000')['values'],{'foreign_fut_qty':5})

    def test_non_numeric_value_is_rejected_not_dropped(self):
        with self.assertRaises(ValueError):flow_packet('asof,foreign_fut_qty,inst_fut_qty\n'+self.at.isoformat()+',12,abc')


if __name__=='__main__':unittest.main()
