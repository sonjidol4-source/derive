import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from common import config
from price_oi import reading,rules
from close_summary import add_extras,build_record,futures_close,save,view,load as load_days
from test_close_summary import series,TIMELINE_1016

CFG=config()


def joined(r):
    return ' '.join(r['inferences'])


class JulyCaseTests(unittest.TestCase):
    """2026-07 카이로스 분석 수치(사용자 대화 기록에서 복원, 거래소 원자료와 재대조 전)를 그대로 넣는다."""
    def test_0724_foreign_buy_with_falling_oi_reads_as_short_cover(self):
        r=reading('day',12701,-1921,0.5,-32800,None,CFG)
        self.assertIn('시장 미결제 감소',r['inferences'][0]);self.assertIn('구분할 수 없습니다',r['inferences'][0]);self.assertEqual(r['side'],1)
        self.assertIn('반대 방향',joined(r));self.assertIn('확인할 수 없습니다',joined(r))
        self.assertNotIn('청산일 가능성',joined(r))
        self.assertTrue(any('시장 전체 값' in t for t in r['limits']))
        self.assertIn('외국인 현물 −3.28조원'.replace('−','-'),' '.join(r['facts']))

    def test_0727_foreign_sell_taken_by_institutions(self):
        r=reading('day',-3434,3489,1.2,-27039,3513,CFG)
        self.assertIn('순매도 · 시장 미결제 증가',r['inferences'][0])
        self.assertIn('실제 거래 상대방은 알 수 없음',joined(r));self.assertIn('누가 움직였는지는',joined(r))
        for word in ('받아낸','받아냈','주도한 것으로','신규 매도(숏) 유입'):self.assertNotIn(word,joined(r))

    def test_0728_new_short_inflow(self):
        r=reading('day',-1743,978,-0.5,-14800,None,CFG)
        self.assertIn('순매도 · 시장 미결제 증가',r['inferences'][0]);self.assertEqual(r['side'],-1)


class RuleTests(unittest.TestCase):
    def test_small_foreign_or_small_oi_holds_back(self):
        self.assertIn('보류',reading('day',200,-1921,0.5,None,None,CFG)['headline'])
        self.assertIn('손바뀜',reading('day',5000,100,0.5,None,None,CFG)['headline'])
        self.assertIn('해석하지 않습니다',reading('m5',None,150,0.2,None,None,CFG)['headline'])

    def test_missing_spot_is_a_stated_limit_and_long_cases(self):
        r=reading('m15',900,300,0.3,None,None,CFG)
        self.assertIn('순매수 · 시장 미결제 증가',r['headline']);self.assertTrue(any('현물 수급이 없어' in t for t in r['limits']))
        self.assertIn('순매도 · 시장 미결제 감소',reading('m15',-900,-300,-0.3,None,None,CFG)['headline'])
        self.assertIn('헤지·차익인지는',joined(reading('day',-2000,800,-0.4,1500,None,CFG)))

    def test_partial_config_is_filled_with_defaults(self):
        r=rules({'rules':{'price_oi':{'oi':{'day':700}}}})
        self.assertEqual(r['oi'],{'m5':100,'m15':200,'day':700});self.assertEqual(r['foreign']['day'],1000)


class FuturesCloseTests(unittest.TestCase):
    def snapshot(self,d,received,**values):
        Path(d,'snapshot_futures.json').write_text(json.dumps({'snapshot':{'received_at':received,'code':'A016C000',
            'values':{'fut_price':859.15,'fut_prev_close':893.05,'fut_oi':145115,'fut_oi_prev':-398,**values}}}),encoding='utf-8')

    def test_after_close_snapshot_preferred_then_intraday_row(self):
        row={'fut_price':860.0,'fut_prev_close':893.05,'fut_oi':145000,'fut_oi_prev':-500,'fut_code':'A016C000',
             'observed_at':{'fut_oi':'2026-10-22T15:49:30+09:00'}}
        with tempfile.TemporaryDirectory() as d:
            self.snapshot(d,'2026-10-22T15:49:40+09:00')
            f=futures_close(d,row,'2026-10-22')
            self.assertEqual(f['source'],'LS t2111 장 마감 후 조회');self.assertEqual(f['oi_change'],-398)
            self.assertAlmostEqual(f['price_pct'],(859.15/893.05-1)*100)
            self.snapshot(d,'2026-10-22T14:00:00+09:00')       # 마감 전 조회본은 쓰지 않는다
            self.assertEqual(futures_close(d,row,'2026-10-22')['source'],'장중 마지막 기록')
            self.assertIsNone(futures_close(d,row,'2026-10-21'))  # 날짜가 다르면 붙이지 않는다

    def test_close_view_has_daily_oi_columns_and_reading(self):
        r=build_record(dt.date(2026,10,16),series(TIMELINE_1016),series(TIMELINE_1016),'x')
        add_extras(r,{'foreign_spot_amt':-2297,'asof':'2026-10-16T15:20:00+09:00'},None,
                   {'price':859.15,'prev_close':893.05,'price_pct':-3.8,'oi':145115,'oi_change':-1500,
                    'observed_at':'2026-10-16T15:49:40','source':'LS t2111 장 마감 후 조회','code':'A016C000'})
        with tempfile.TemporaryDirectory() as d:
            save(r,d);v=view(d,cfg=CFG)
        row=v['rows'][0];self.assertEqual(row['oi_change'],-1500);self.assertEqual(row['quadrant'],'가격↓·미결제↓')
        self.assertIn('미결제 −1,500계약 → 가격↓·미결제↓',v['latest']['text'])
        # 외국인 −4,200 순매도 + OI 감소 → 기존 롱 정리, 기관계 +4,200이 반대편
        R=v['latest']['reading'];self.assertIn('순매도 · 시장 미결제 감소',R['headline']);self.assertIn('기관계 선물 +4,200계약',' '.join(R['inferences']))

    def test_0724_late_reversal_is_read_with_oi_of_that_window(self):
        # 장중 −13,460계약 → 마감 −759계약. 당일 순매매(−759)만 보면 '보류'지만 반전 구간은 +12,701계약.
        q=series({'08:46:00':-2000,'14:30:00':-13460,'15:40:00':-759})
        r=build_record(dt.date(2026,7,24),q,q,'x')
        self.assertEqual(r['foreign_path']['low'],-13460);self.assertEqual(r['foreign_path']['low_time'],'15:39:30')
        ticks=[{'at':'2026-07-24T15:39:05.120000+09:00','values':{'fut_oi':{'value':162666},'fut_price':{'value':398.0}}}]
        with tempfile.TemporaryDirectory() as d:
            row={'fut_price':402.5,'fut_prev_close':400.1,'fut_oi':160745,'fut_oi_prev':-1921,'observed_at':{'fut_oi':'2026-07-24T15:49:40+09:00'}}
            fut=futures_close(d,row,'2026-07-24',ticks,(r['foreign_path']['low_time'],r['foreign_path']['high_time']))
            self.assertEqual(fut['marks']['15:39:30']['oi'],162666)
            add_extras(r,{'foreign_spot_amt':-32800,'asof':'2026-07-24T18:00:00+09:00','source':'REST 마감 후 조회 ka10051'},None,fut)
            save(r,d);v=view(d,cfg=CFG)
        self.assertIn('보류',v['latest']['reading']['headline'])
        S=v['latest']['swing_reading']
        self.assertIn('누적 순매수 12,701계약 증가',S['title']);self.assertIn('순매수 · 시장 미결제 감소',S['headline'])
        self.assertIn('반대 방향',' '.join(S['inferences']));self.assertIn('미결제 162,666',S['facts'][0])
        self.assertIn('-1,921계약',' '.join(S['facts']))

    def test_steady_selling_has_no_reversal_and_missing_mark_is_a_limit(self):
        steady=series({'08:46:00':-500,'11:00:00':-2500,'14:00:00':-4200})
        r=build_record(dt.date(2026,10,16),steady,steady,'x');r['futures']={'oi':150000,'price':850.0,'marks':{}}
        from close_summary import swing_reading
        self.assertIsNone(swing_reading(r,CFG))
        q=series({'08:46:00':-2000,'14:30:00':-13460,'15:40:00':-759})
        r=build_record(dt.date(2026,7,24),q,q,'x');r['futures']={'oi':160745,'price':402.5,'marks':{}}
        S=swing_reading(r,CFG);self.assertIn('미결제 기록이 없어',S['limits'][0]);self.assertIn('구분하지 않습니다',S['headline'])

    def test_requery_keeps_earlier_low_cut_off_by_500_row_limit(self):
        full=series({'08:46:00':-500,'12:40:00':-1657,'12:41:00':-900,'14:48:00':-1555,'15:40:00':1552})
        cut=[r for r in full if r['time']>='14000000']
        with tempfile.TemporaryDirectory() as d:
            first=build_record(dt.date(2026,10,22),full,full,'a');first['futures']={'oi':143707,'price':857.0,'marks':{'12:40:30':{'oi':145400,'price':870.0}}}
            save(first,d)
            again=build_record(dt.date(2026,10,22),cut,cut,'b');again['futures']={'oi':143707,'price':857.0,'marks':{'14:48:30':{'oi':145100,'price':862.0}}}
            self.assertEqual(again['foreign_path']['low'],-1555)
            save(again,d);saved=load_days(d)['2026-10-22']
        self.assertEqual(saved['foreign_path']['low'],-1657);self.assertEqual(saved['foreign_path']['low_time'],'12:40:30')
        self.assertEqual(set(saved['futures']['marks']),{'12:40:30','14:48:30'})

    def test_no_futures_means_no_reading(self):
        r=build_record(dt.date(2026,10,16),series(TIMELINE_1016),series(TIMELINE_1016),'x')
        with tempfile.TemporaryDirectory() as d:
            save(r,d);v=view(d)
        self.assertIsNone(v['latest']['reading']);self.assertIsNone(v['rows'][0]['quadrant'])


if __name__=='__main__':unittest.main()
