import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from common import config,stamp,number,nearest_month,monthly_expiry,business_day
from engine import Engine,price_oi_quadrant
from demo import populate
from positions import parse_positions,scenarios
from tr_schema import parse_spec,extract_source,normalize_strikes,validate_source
from rest_feed import program_packet,spot_packet,index_packet,read_env
from importers import flow_packet

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.cfg=config();self.e=Engine(self.cfg,self.temp.name)
        self.at=stamp('2026-10-14T10:00:00+09:00')
    def tearDown(self):self.e.store.close();self.temp.cleanup()
    def packet(self,at,**values):
        return {'asof':at.isoformat(),'source':'test','fut_code':'F1','values':values}
    def fill(self,minutes=20):
        for i in range(minutes+1):
            at=self.at+dt.timedelta(minutes=i)
            self.e.ingest(self.packet(at,k200=500+i*.12,foreign_fut_qty=-5000+i*100,inst_fut_qty=500-i*10,
                nonarb_amt=-3000+i*50,basis=1+i*.01,basis_theory=1.2,fut_oi=100000+i*10,fut_oi_prev=i*10),at)
            result=self.e.compute(at)
        return result
    def test_price_oi_quadrants(self):
        q=lambda p,o:price_oi_quadrant(p,o,.1,100)['label']
        self.assertEqual([q(.2,150),q(.2,-150),q(-.2,150),q(-.2,-150)],['가격↑·미결제↑','가격↑·미결제↓','가격↓·미결제↑','가격↓·미결제↓'])
        self.assertEqual([q(.05,150),q(.2,50),q(.05,-50),q(None,150)],['미결제만 변화','가격만 움직임','변화 작음','자료 부족'])
        self.assertEqual(price_oi_quadrant(-.2,150,.1,100)['side'],-1)
    def test_price_oi_view_windows_and_day(self):
        for i in range(16):
            at=self.at+dt.timedelta(minutes=i)
            self.e.ingest(self.packet(at,k200=500+i*.2,fut_price=501+i*.2,fut_prev_close=498,fut_oi=100000-i*30,fut_oi_prev=-1500-i*30,foreign_fut_qty=i*80),at)
            s=self.e.compute(at)
        w={x['key']:x for x in s['price_oi']['windows']}
        self.assertEqual(w['m5']['oi_change'],-150);self.assertEqual(w['m5']['label'],'가격↑·미결제↓');self.assertEqual(w['m5']['foreign_fut'],400)
        self.assertEqual(w['m15']['label'],'가격↑·미결제↓')
        self.assertAlmostEqual(w['day']['price_pct'],(504/498-1)*100);self.assertEqual(w['day']['oi_change'],-1950);self.assertEqual(w['day']['label'],'가격↑·미결제↓')
        self.assertEqual(s['price_oi']['rollover'],'')
        self.assertEqual([x['reading']['window'] for x in s['price_oi']['windows']],['m5','m15','day'])
        self.assertIn('외국인 선물 순매수 · 시장 미결제 감소',w['day']['reading']['headline'])  # 당일 외국인 +1,200·OI −1,950
        self.assertIn('외국인 선물 순매수 · 시장 미결제 감소',w['m5']['reading']['headline'])   # 5분 +400·OI −150
        self.assertTrue(any('현물 수급이 없어' in t for t in w['m5']['reading']['limits']))
        self.assertTrue(any('투자자별 신규·청산 구분 불가' in t for t in s['verdict']['facts']))
        self.assertIsNotNone(s['history'][-1]['values']['fut_oi'])
    def test_price_oi_missing_prev_close_and_rollover_note(self):
        at=stamp('2026-12-04T10:00:00+09:00')
        self.e.ingest(self.packet(at,k200=500,fut_price=501,fut_oi=100000,fut_oi_prev=-2000),at);s=self.e.compute(at)
        day=s['price_oi']['windows'][2]
        self.assertIsNone(day['price_pct']);self.assertEqual(day['label'],'자료 부족')
        self.assertIn('2026-12-10',s['price_oi']['rollover'])
        self.assertIsNone(s['price_oi']['windows'][0]['oi_change'])
    def test_missing_is_not_zero(self):
        s=self.e.compute(self.at);self.assertIsNone(s['metrics']['foreign_fut_qty']['day']);self.assertEqual(s['verdict']['side'],0)
    def test_stale_does_not_confirm(self):
        self.e.ingest(self.packet(self.at,k200=500,foreign_fut_qty=2000,nonarb_amt=1000),self.at)
        self.e.compute(self.at);s=self.e.compute(self.at+dt.timedelta(minutes=21))
        self.assertIsNone(s['metrics']['foreign_fut_qty']['day']);self.assertEqual(s['verdict']['side'],0);self.assertEqual(s['events'],[])
    def test_positive_5m_with_negative_day(self):
        s=self.fill();self.assertLess(s['metrics']['foreign_fut_qty']['day'],0);self.assertEqual(s['metrics']['foreign_fut_qty']['m5'],500)
        self.assertEqual(s['verdict']['label'],'매수 흐름 동조');self.assertTrue(s['events'])
    def test_market_oi_never_attributed_to_foreign(self):
        s=self.fill();self.assertFalse(any('신규 롱' in t or '숏 커버' in t for t in s['verdict']['facts']))
    def test_interruption_invalidates_window(self):
        self.fill(6);later=self.at+dt.timedelta(minutes=12)
        self.e.ingest(self.packet(later,k200=502,foreign_fut_qty=0,nonarb_amt=0),later);s=self.e.compute(later)
        self.assertIsNone(s['metrics']['foreign_fut_qty']['m5'])
    def test_stale_chart_points_are_null(self):
        self.e.ingest(self.packet(self.at,k200=500,foreign_fut_qty=100),self.at);self.e.compute(self.at)
        s=self.e.compute(self.at+dt.timedelta(minutes=4));self.assertIsNone(s['history'][-1]['values']['foreign_fut_qty'])
    def test_date_rollover_clears_values(self):
        self.fill();s=self.e.compute(self.at+dt.timedelta(days=1));self.assertIsNone(s['metrics']['foreign_fut_qty']['day']);self.assertEqual(self.e.fut_code,'')
    def test_partial_expiry_history_is_not_complete(self):
        s=self.fill();self.assertFalse(s['cumulative']['complete']);self.assertGreater(len(s['cumulative']['missing']),0)
    def test_old_and_future_packets_rejected(self):
        self.assertFalse(self.e.ingest(self.packet(self.at-dt.timedelta(days=1),k200=5),self.at))
        self.assertFalse(self.e.ingest(self.packet(self.at+dt.timedelta(minutes=1),k200=5),self.at))
    def test_out_of_order_does_not_overwrite(self):
        self.e.ingest(self.packet(self.at,k200=500),self.at)
        self.e.ingest(self.packet(self.at-dt.timedelta(seconds=5),k200=1),self.at)
        self.assertEqual(self.e.val('k200',self.at),500)
    def test_repeated_compute_no_duplicate_minute(self):
        self.fill(3);self.e.compute(self.at+dt.timedelta(minutes=3,seconds=10));self.assertEqual(len(self.e.history),4)
    def test_contract_rollover_no_jump_signal(self):
        self.fill(8);at=self.at+dt.timedelta(minutes=9)
        packet=self.packet(at,k200=502,foreign_fut_qty=100000,nonarb_amt=10);packet['fut_code']='F2'
        self.e.ingest(packet,at);s=self.e.compute(at);self.assertIsNone(s['metrics']['foreign_fut_qty']['m5'])
    def test_forward_results_wait_until_horizon(self):
        self.fill(9);events=self.e.store.events();self.assertTrue(events)
        self.assertNotIn('15',events[0]['outcomes'])
        self.fill(30);events=self.e.store.events();self.assertTrue(any('15' in e['outcomes'] for e in events))
    def test_restart_restores_original_freshness(self):
        self.fill(6);self.e.store.close();self.e=Engine(self.cfg,self.temp.name)
        s=self.e.compute(self.at+dt.timedelta(minutes=20));self.assertIsNone(s['metrics']['foreign_fut_qty']['day'])
    def test_basis_residual_and_stale_theory(self):
        s=self.fill(6);self.assertAlmostEqual(s['metrics']['basis_residual']['day'],-.14)
        at=self.at+dt.timedelta(minutes=7)
        self.e.ingest(self.packet(at,k200=500,basis=2),at);s=self.e.compute(at)
        self.assertIsNone(s['metrics']['basis_residual']['day'])
    def test_demo_contains_signal_and_stale_scenario(self):
        snapshots=list(populate(self.e));self.assertTrue(snapshots[-1]['events'])
        self.assertIsNone(snapshots[93]['metrics']['foreign_fut_qty']['day'])
    def test_day_last_observation_is_not_erased_by_later_stale_tick(self):
        self.fill(6);self.e.compute(self.at+dt.timedelta(minutes=15))
        daily=self.e.store.daily()[self.at.date().isoformat()]
        self.assertEqual(daily['foreign_fut_qty'],-4400)
        self.assertFalse(daily['finalized'])
    def test_horizon_after_index_close_excluded(self):
        at=stamp('2026-10-14T15:20:00+09:00');self.e.new_day(at)
        self.e.store.event({'id':'testclosing','at':at.isoformat(),'kind':'매수 흐름 동조','side':1,'price':500,'facts':[]})
        self.e.evaluate_events(at)
        outcomes=self.e.store.events()[0]['outcomes']
        self.assertEqual(outcomes['15']['status'],'장외');self.assertNotIn('5',outcomes)

class ContractTests(unittest.TestCase):
    def test_number_signed_missing_nonfinite(self):
        self.assertEqual(number('--1,200'),-1200);self.assertEqual(number('+3.5'),3.5)
        for x in ('','nan','inf',None,True):self.assertIsNone(number(x))
    def test_month_list_first_is_not_nearest(self):
        cfg=config();at=stamp('2026-10-15T10:00:00+09:00')
        self.assertEqual(nearest_month(['202612','202611','202610','202610','202611','202612'],at,cfg),'202611')
    def test_expiry_cutoff_and_holiday(self):
        cfg=config();cfg['calendar']['holidays'].append('2026-11-12')
        self.assertEqual(monthly_expiry('202611',cfg),dt.date(2026,11,11))
        self.assertEqual(nearest_month(['202611','202612'],stamp('2026-11-11T15:30:00+09:00'),cfg),'202612')
        self.assertFalse(business_day(dt.date(2026,7,17),cfg))
    def test_integer_and_decimal_strike_units(self):
        self.assertEqual(normalize_strikes(['50000','50250'],501)[0],[500,502.5])
        self.assertEqual(normalize_strikes(['500.00','502.50'],501)[0],[500,502.5])
    def test_tr_schema_and_selected_rows(self):
        spec=parse_spec('[TRINFO]\nTRName=투자자별조회\n[INPUT]\n@START_입력\n시장=0\n@END\n[OUTPUT]\n@START_결과\n주체=0\n매수=0\n매도=0\n@END','opt99999')
        src={'trcode':'opt99999','scope':'cumulative_day','inputs':{'시장':'3'},'fields':{'foreign_fut_qty':{'record':'결과','field':'매수','subtract':'매도','scale':1,'where':{'field':'주체','equals':'외국인'}}}}
        validate_source(src,spec)
        vals=extract_source(src,{'결과':[{'주체':'기관','매수':'1','매도':'2'},{'주체':'외국인','매수':'2,000','매도':'1,500'}]})
        self.assertEqual(vals['foreign_fut_qty'],500)
    def test_tr_missing_inputs_rejected(self):
        spec={'trcode':'opt99999','title':'투자자조회','inputs':['시장'],'records':{'결과':['외국인']}}
        with self.assertRaises(ValueError):validate_source({'trcode':'opt99999','inputs':{},'fields':{}},spec)
    def test_program_units_and_actual_timestamp(self):
        at=stamp('2026-10-14T10:05:30+09:00')
        packet=program_packet({'prm_trde_trnsn':[{'cntr_tm':'100300','ndiffpro_trde_netprps':'-20000','dfrt_trde_netprps':'3000','all_netprps':'-17000'},{'cntr_tm':'100500','ndiffpro_trde_netprps':'+30000','dfrt_trde_netprps':'0','all_netprps':'30000'}]},at)
        self.assertEqual(packet['values']['nonarb_amt'],300);self.assertIn('10:05:00',packet['asof'])
    def test_spot_selects_market_not_first_row(self):
        packet=spot_packet({'inds_netprps':[{'inds_cd':'002','frgnr_netprps':'999'},{'inds_cd':'001','frgnr_netprps':'-300','orgn_netprps':'200','sc_netprps':'30','ind_netprps':'100'}]},stamp('2026-10-14T10:00:00+09:00'))
        self.assertEqual(packet['values']['foreign_spot_amt'],-300)
    def test_breadth_uses_total_including_flat(self):
        p=index_packet({'rising':'10','upl':'2','fall':'4','lst':'1','stdns':'3'},stamp('2026-10-14T10:00:00+09:00'))
        self.assertEqual(p['values']['breadth'],60)
    def test_positions_disallow_daily_trade_flows(self):
        text='investor,kind,expiry,strike,net_qty,avg_price,asof,source,scope\n외국인,C,2026-11-12,500,100,5,2026-10-14T10:00:00+09:00,HTS,daily_flow'
        with self.assertRaises(ValueError):parse_positions(text)
    def test_same_expiry_payoff_and_delta(self):
        text='investor,kind,expiry,strike,net_qty,avg_price,delta,asof,source,scope\n외국인,C,2026-11-12,500,100,5,0.5,2026-10-14T10:00:00+09:00,HTS,broker_estimate'
        rows=parse_positions(text);groups=scenarios(rows,500,stamp('2026-10-14T10:05:00+09:00'))['groups']
        self.assertEqual(groups[0]['delta_contracts'],50);self.assertEqual(groups[0]['pnl_eok'][8],-1.25)
    def test_positions_mixed_timestamps_rejected(self):
        text='investor,kind,expiry,strike,net_qty,avg_price,delta,asof,source,scope\n외국인,C,2026-11-12,500,100,5,0.5,2026-10-14T10:00:00+09:00,HTS,broker_estimate\n외국인,P,2026-11-12,500,-100,5,-0.5,2026-10-14T10:02:00+09:00,HTS,broker_estimate'
        with self.assertRaises(ValueError):parse_positions(text)
    def test_credentials_aliases_reused_without_copy(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'settings.env';p.write_text('KIWOOM_APP_KEY=placeholder-key\nKIWOOM_SECRET_KEY=placeholder-secret\n')
            values=read_env(p);self.assertEqual(values['KIWOOM_APP_KEY'],'placeholder-key')
    def test_flow_csv_latest_timestamp(self):
        text='asof,foreign_fut_qty\n2026-10-14T10:00:00+09:00,-100\n2026-10-14T10:01:00+09:00,200'
        self.assertEqual(flow_packet(text)['values']['foreign_fut_qty'],200)
    def test_position_product_mismatch_rejected(self):
        text='investor,kind,expiry,strike,net_qty,avg_price,asof,source,scope,product\n외국인,C,2026-11-12,500,100,5,2026-10-14T10:00:00+09:00,HTS,broker_estimate,MINIKOSPI200'
        with self.assertRaises(ValueError):parse_positions(text)
    def test_cumulative_holdings_cannot_be_mapped_as_daily_flow(self):
        spec={'trcode':'opt99999','title':'투자자조회','inputs':['시장'],'records':{'결과':['외국인']}}
        source={'trcode':'opt99999','scope':'estimated_holdings','inputs':{'시장':'3'},'fields':{'foreign_fut_qty':{'record':'결과','field':'외국인'}}}
        with self.assertRaises(ValueError):validate_source(source,spec)

class OcxCallbackTests(unittest.TestCase):
    def test_data_copied_inside_callback_and_old_response_ignored(self):
        import sys,types,importlib
        qax=types.ModuleType('PyQt5.QAxContainer');qax.QAxWidget=object
        qt=types.ModuleType('PyQt5.QtCore');qt.QEventLoop=object;qt.QTimer=object
        original={k:sys.modules.get(k) for k in ('PyQt5','PyQt5.QAxContainer','PyQt5.QtCore')}
        sys.modules['PyQt5']=types.ModuleType('PyQt5');sys.modules['PyQt5.QAxContainer']=qax;sys.modules['PyQt5.QtCore']=qt
        try:
            from kiwoom import Kiwoom
            k=Kiwoom.__new__(Kiwoom);done=[];calls=[]
            k.active={'rq':'radar_2','spec':{'trcode':'opt99999','records':{'結果':['外國人']}},'done':lambda data,msg:done.append(data)}
            k.timeout=types.SimpleNamespace(stop=lambda:None)
            k.call=lambda sig,*args:(calls.append(sig) or (1 if 'GetRepeatCnt' in sig else '500'))
            k._tr('7101','radar_1','opt99999','結果','0');self.assertEqual(calls,[])
            k._tr('7101','radar_2','opt99999','結果','0');self.assertEqual(done[0]['結果'][0]['外國人'],'500');self.assertIsNone(k.active)
        finally:
            for key,val in original.items():
                if val is None:sys.modules.pop(key,None)
                else:sys.modules[key]=val

if __name__=='__main__':unittest.main()
