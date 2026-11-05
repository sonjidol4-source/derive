import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from common import ROOT,config,stamp
from engine import Engine
from ls_feed import AMT_KEYS,QTY_KEYS,ls_packet,program_packet,row_time
from rest_feed import ls_program

RECEIVED=stamp('2026-10-16T18:11:00+09:00')


def body(rows,code='00000'):
    return {'rsp_cd':code,'rsp_msg':'','t1602OutBlock':{},'t1602OutBlock1':rows}


class LsPacketTests(unittest.TestCase):
    def test_saved_real_response_maps_to_contracts(self):
        path=ROOT/'data'/'ls_t1602_probe_today_qty.json'
        if not path.exists():self.skipTest('실조회 저장본 없음')
        p=ls_packet(json.loads(path.read_text(encoding='utf-8'))['response'],QTY_KEYS,RECEIVED)
        # 2026-10-16 증권사 화면과 대조된 값
        self.assertEqual((p['values']['foreign_fut_qty'],p['values']['inst_fut_qty'],p['values']['individual_fut_qty']),(-4200,5077,-1099))
        self.assertEqual(p['asof'][11:19],'18:10:30');self.assertEqual(p['source'],'LS t1602')

    def test_amount_keys_and_missing_values_are_not_zero(self):
        p=ls_packet(body([{'time':'10003000','sv_17':-9540,'sv_18':None,'sv_08':-2414}]),AMT_KEYS,RECEIVED)
        self.assertEqual(p['values'],{'foreign_fut_amt':-9540,'individual_fut_amt':-2414})

    def test_errors_are_raised_not_swallowed(self):
        with self.assertRaises(ValueError):ls_packet(body([],'00000'),QTY_KEYS,RECEIVED)
        with self.assertRaises(ValueError):ls_packet(body([{'time':'10000000','sv_17':1}],'IGW00121'),QTY_KEYS,RECEIVED)
        with self.assertRaises(ValueError):ls_packet(body([{'time':'23595900','sv_17':1}]),QTY_KEYS,RECEIVED)  # 미래 시각
        self.assertIsNone(row_time('ab',RECEIVED.date()))


class ProgramPacketTests(unittest.TestCase):
    AT=stamp('2026-10-22T20:06:00+09:00')

    def test_saved_real_response_matches_kiwoom_values(self):
        path=ROOT/'data'/'ls_stock_probe.json'
        if not path.exists():self.skipTest('실조회 저장본 없음')
        saved=json.loads(path.read_text(encoding='utf-8'))
        r=saved['ls'].get('t1632_cumulative') or saved['ls']['t1632']
        p=program_packet(r['response'],self.AT)
        # 2026-10-22 키움 ka90005 −329,420 · −6,322 · −323,097(백만원)과 같은 값
        self.assertAlmostEqual(p['values']['program_amt'],-3294.20);self.assertAlmostEqual(p['values']['arb_amt'],-63.22)
        self.assertAlmostEqual(p['values']['nonarb_amt'],-3230.97);self.assertEqual(p['asof'][11:19],'20:05:25')

    def test_other_day_future_time_and_errors_rejected(self):
        row={'time':'100000','tot3':-1000,'cha3':0,'bcha3':-1000}
        ok={'rsp_cd':'00000','t1632OutBlock':{'date':'20261022'},'t1632OutBlock1':[row]}
        self.assertEqual(program_packet(ok,self.AT)['values'],{'program_amt':-10.0,'arb_amt':0.0,'nonarb_amt':-10.0})
        with self.assertRaises(ValueError):program_packet({**ok,'t1632OutBlock':{'date':'20261021'}},self.AT)
        with self.assertRaises(ValueError):program_packet({**ok,'t1632OutBlock1':[{**row,'time':'235959'}]},self.AT)
        with self.assertRaises(ValueError):program_packet({**ok,'rsp_cd':'IGW00121'},self.AT)
        with self.assertRaises(ValueError):program_packet({**ok,'t1632OutBlock1':[{'time':'100000'}]},self.AT)

    def test_switch_needs_ls_enabled(self):
        self.assertTrue(ls_program({'ls':{'enabled':True,'program':True}}))
        self.assertFalse(ls_program({'ls':{'enabled':False,'program':True}}))
        self.assertFalse(ls_program({'ls':{'enabled':True}}))


class LsEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.e=Engine(config(),self.temp.name)
    def tearDown(self):self.e.store.close();self.temp.cleanup()

    def test_thirty_second_series_gives_change_and_opposite_flow_fact(self):
        t0=stamp('2026-10-15T10:00:00+09:00')
        for i in range(0,13):  # 30초 간격, 외국인 계약이 30초마다 -50
            at=t0+dt.timedelta(seconds=30*i)
            hhmmss=at.strftime('%H%M%S')+'00'
            self.e.ingest(ls_packet(body([{'time':hhmmss,'sv_17':-1000-50*i}]),QTY_KEYS,at),at)
            self.e.ingest(ls_packet(body([{'time':hhmmss,'sv_17':-2000-100*i}]),AMT_KEYS,at),at)
            self.e.ingest({'asof':at.isoformat(),'source':'REST ka10051','values':{'foreign_spot_amt':3000,'k200':860},'quality':'장중 잠정'},at)
            if i%2==0:s=self.e.compute(at)
        self.assertEqual(s['metrics']['foreign_fut_qty']['m5'],-500)
        row=[r for r in s['flows'] if r['investor']=='외국인'][0]
        self.assertEqual(row['fut_amt']['value'],-3200);self.assertIsNone(row['fut_manual_change'])
        self.assertTrue(any('현물·선물 반대 방향, 거래 목적은 식별 불가' in f for f in s['verdict']['facts']))


if __name__=='__main__':unittest.main()
