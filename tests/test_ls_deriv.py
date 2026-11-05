import json
import tempfile
import unittest
from pathlib import Path
from common import config,stamp
from engine import Engine
import ls_deriv as L
from option_analytics import build

AT=stamp('2026-10-22T10:00:00+09:00')
T8467={'rsp_cd':'00000','t8467OutBlock':[{'hname':'SP 2612-2703','shcode':'D016CC3S'},{'hname':'F 2612','shcode':'A016C000'},{'hname':'F 2703','shcode':'A016D000'}]}
T2111={'rsp_cd':'00000','t2111OutBlock':{'price':'893.05','mgjv':145513,'mgjvdiff':-353,'sbasis':'2.01','ibasis':'3.38','kospijisu':'891.04','jnilclose':'880.10'}}


def board(strikes=(880.0,890.0,900.0),oi=(100,500,300)):
    calls=[{'optcode':'B016BC%03d'%i,'actprice':'%.2f'%k,'price':'5.0','mgjv':o,'iv':'20.0','delt':'0.55'} for i,(k,o) in enumerate(zip(strikes,oi))]
    puts=[{'optcode':'C016BC%03d'%i,'actprice':'%.2f'%k,'price':'4.0','mgjv':o*2,'iv':'21.0','delt':'-0.45'} for i,(k,o) in enumerate(zip(strikes,oi))]
    return {'rsp_cd':'00000','t2301OutBlock':{},'t2301OutBlock1':calls,'t2301OutBlock2':puts}


def investors(sv):
    return {'rsp_cd':'00000','t1602OutBlock1':[{'time':'09595900',**{'sv_'+k:v for k,v in sv.items()}}]}


class ParseTests(unittest.TestCase):
    def test_near_future_skips_spreads(self):
        self.assertEqual(L.near_future(T8467),'A016C000')
        with self.assertRaises(ValueError):L.near_future({'t8467OutBlock':[{'hname':'SP 2612-2703','shcode':'x'}]})

    def test_futures_values_map_basis_and_k200(self):
        v=L.futures_values(T2111)
        self.assertEqual(v,{'fut_price':893.05,'fut_oi':145513,'fut_oi_prev':-353,'basis':2.01,'basis_theory':3.38,'k200':891.04,'fut_prev_close':880.10})
        with self.assertRaises(ValueError):L.futures_values({'t2111OutBlock':{'price':'0'}})

    def test_option_board_keeps_kiwoom_delta_units(self):
        calls,puts=L.option_snapshots(board(),'202611',AT.isoformat())
        self.assertEqual((calls['kind'],puts['kind'],len(calls['rows'])),('calls','puts',3))
        self.assertAlmostEqual(calls['rows'][0]['delta_raw'],55.0);self.assertAlmostEqual(puts['rows'][0]['delta_raw'],-45.0)
        self.assertTrue(calls['complete']);self.assertEqual(calls['listed_range'],[880.0,900.0])
        b=board();b['t2301OutBlock2']=b['t2301OutBlock2'][:2]
        self.assertFalse(L.option_snapshots(b,'202611',AT.isoformat())[0]['complete'])  # 콜·풋 수가 다르면 보류

    def test_option_month_rolls_after_expiry(self):
        cfg=config()
        self.assertEqual(L.option_months(stamp('2026-11-01T10:00:00+09:00'),cfg),'202611')
        self.assertEqual(L.option_months(stamp('2026-11-12T15:30:00+09:00'),cfg),'202612')  # 11/12 만기 15:20 이후

    def test_investor_report_uses_confirmed_call_put(self):
        # 2026-10-22 한투 콜(OC01)·풋(OP01)과 8개 값 일치 → 시장구분 5 = 풋, 6 = 콜
        m5,m6=L.OPTION_MARKETS
        self.assertEqual((m5['market'],m5['guess'],m6['market'],m6['guess']),('5','풋','6','콜'))
        r=L.option_investor_report([(m5,investors({'17':490,'18':-538,'08':99}),investors({'17':12})),(m6,investors({'17':-377}),None)],AT,config())
        self.assertTrue(r['mapping_confirmed'])
        self.assertEqual((r['groups'][0]['label'],r['groups'][1]['label']),('풋옵션','콜옵션'));self.assertNotIn('미확정',r['note'])
        self.assertEqual(r['groups'][0]['qty'],{'foreign':490,'inst':-538,'individual':99})
        self.assertEqual(r['groups'][1]['amt'],{})

    def test_old_saved_labels_are_relabelled(self):
        self.assertEqual(L.relabel('시장구분 5 (콜 추정 · 콜·풋 미확정): 외국인 +541계약'),'풋옵션: 외국인 +541계약')
        self.assertEqual(L.relabel('시장구분 6 (풋 추정 · 콜·풋 미확정): 외국인 -3,485계약'),'콜옵션: 외국인 -3,485계약')


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.dir=Path(self.temp.name)
        self.e=Engine(config(),self.dir);self.calls=[]
        responses={'t8467':T8467,'t2111':T2111,'t2301':board()}
        def call(path,tr,block):
            self.calls.append((tr,block.get('market')))
            if tr=='t1602':return investors({'17':490 if block['market']=='5' else -377,'18':1,'08':2})
            return responses[tr]
        self.d=L.Derivatives(config(),self.e,call,lambda:None,self.dir)
    def tearDown(self):self.e.store.close();self.temp.cleanup()

    def test_session_tick_feeds_engine_and_files(self):
        self.d.session_tick(AT,lambda:AT,option_due=True)
        self.assertEqual([c[0] for c in self.calls],['t8467','t2111','t2301','t1602','t1602','t1602','t1602'])
        self.assertEqual(self.e.values['fut_price']['source'],'LS t2111');self.assertEqual(self.e.fut_code,'A016C000')
        self.assertIn('calls',self.e.market_snapshots)
        for name in ('snapshot_futures.json','snapshot_calls.json','snapshot_puts.json','ls_option_investors.json'):
            self.assertTrue((self.dir/name).exists(),name)
        self.calls.clear();self.d.session_tick(AT,lambda:AT,option_due=False)
        self.assertEqual([c[0] for c in self.calls],['t2111'])  # 근월물 코드는 하루 한 번

    def test_analytics_without_kiwoom_investors(self):
        self.d.session_tick(AT,lambda:AT,option_due=True)
        A=build(self.dir,891.04,'test',{},config(),AT)
        self.assertEqual(A['status'],'received');self.assertEqual(A['flows'],[])
        self.assertTrue(A['structure']['complete']);self.assertEqual(A['structure']['max_pain'],890.0)
        self.assertIn('max pain 890(참고',A['structure_text'])
        self.assertEqual(len(A['ls_totals']['rows']),2);self.assertTrue(A['ls_totals']['confirmed'])
        self.assertIn('외국인 +490계약',A['ls_totals']['rows'][0]['text'])
        self.assertFalse((self.dir/'option_flow_history.jsonl').exists())  # 투자자 델타가 없으면 이력도 남기지 않음

    def test_after_close_once_per_day(self):
        evening=stamp('2026-10-22T18:00:00+09:00')
        self.assertTrue(self.d.after_close(evening,lambda:evening))
        self.calls.clear()
        self.assertFalse(self.d.after_close(evening,lambda:evening));self.assertEqual(self.calls,[])
        # 재시작해도 그날 16:10 이후 LS 저장본이 있으면 다시 부르지 않는다
        fresh=L.Derivatives(config(),self.e,lambda *a:self.fail('호출되면 안 됨'),lambda:None,self.dir)
        self.assertFalse(fresh.after_close(evening,lambda:evening))
        early=stamp('2026-10-22T15:55:00+09:00')
        self.assertFalse(L.Derivatives(config(),self.e,lambda *a:self.fail('16:10 전'),lambda:None,self.dir).after_close(early,lambda:early))


class StaleNoteTests(unittest.TestCase):
    def test_old_saved_note_is_replaced(self):
        from option_analytics import ls_view
        old={'generated_at':'2026-10-22T16:23:35+09:00','data_day':'2026-10-22','source':'LS t1602','mapping_confirmed':False,
             'groups':[{'key':'m5','market':'5','label':'시장구분 5 (콜 추정 · 콜·풋 미확정)','qty':{'foreign':541.0},'amt':{}}],
             'investors':[['foreign','외국인']],'note':'옵션 단위 대조 전. 콜·풋 구분 미확정: 0780과 대조 전'}
        v=ls_view(old,None)
        self.assertNotIn('미확정',v['note']+v['headline']+v['rows'][0]['text'])
        self.assertNotIn('대조 전',v['note'])
        self.assertTrue(v['rows'][0]['text'].startswith('풋옵션'))


if __name__=='__main__':unittest.main()
