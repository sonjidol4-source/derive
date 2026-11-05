import copy
import datetime as dt
import tempfile
import unittest
from common import config, stamp
from engine import Engine
from flow_explanation import flow_speed


class ReboundTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.e=Engine(config(),self.temp.name)
        self.start=stamp('2026-10-30T10:00:00+09:00')

    def tearDown(self):
        self.e.store.close();self.temp.cleanup()

    def run_series(self, until=20, spot_improves=False):
        states=[]
        for i in range(until+1):
            at=self.start+dt.timedelta(minutes=i)
            cash=-70*min(i,10)-20*max(i-10,0)
            spot=-30*i if not spot_improves else -30*min(i,10)-10*max(i-10,0)
            self.e.ingest({'asof':at.isoformat(),'source':'test','scope':'cumulative_day','fut_code':'F1',
                'values':{'k200':500+i*.12,'fut_price':501+i*.12,'fut_prev_close':520,
                          'foreign_fut_qty':-5000+i*50,'nonarb_amt':cash,'foreign_spot_amt':spot,
                          'basis':-.8+i*.02,'basis_theory':.2}},at)
            states.append(self.e.compute(at))
        return states

    def test_small_positive_flow_is_early_signal_not_confirmation(self):
        states=self.run_series(6)
        r=states[-1]['rebound']
        self.assertEqual(r['foreign5'],250)
        self.assertEqual(r['stage'],1)
        self.assertEqual(r['title'],'반등 조짐')
        self.assertFalse(r['confirmed'])
        self.assertTrue(any(c['passed'] is None for c in r['conditions'] if c['stage']==2))

    def test_confirm_requires_time_and_records_once_with_metadata(self):
        states=self.run_series()
        self.assertFalse(states[15]['rebound']['confirmed'])
        self.assertTrue(states[18]['rebound']['confirmed'])
        events=[e for e in self.e.store.events() if e['kind']=='반등 확인']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['rule_context']['method'],'rebound-1')
        self.assertTrue(any(o.get('role')=='rebound_15m' for o in events[0]['input_observations']))
        before=len(self.e.store.events())
        self.e.compute(self.start+dt.timedelta(minutes=20,seconds=10))
        self.assertEqual(len(self.e.store.events()),before)

    def test_sustainability_uses_spot_and_preceding_high(self):
        states=self.run_series(20,spot_improves=True)
        r=states[-1]['rebound']
        self.assertEqual(r['stage'],3)
        self.assertTrue(r['confirmed'])
        self.assertLess(r['prior_high'],self.e.val('k200',self.start+dt.timedelta(minutes=20)))
        self.assertTrue(any(e['kind']=='반등 지속성 확인' for e in self.e.store.events()))

    def test_missing_basis_and_expanding_program_sales_block_confirmation(self):
        self.run_series(15)
        at=self.start+dt.timedelta(minutes=15)
        self.e.values.pop('basis_theory')
        r=self.e.compute(at)['rebound']
        self.assertEqual(r['stage'],1)
        self.assertIsNone(next(c['passed'] for c in r['conditions'] if c['name']=='베이시스 개선'))
        self.e.values['nonarb_amt']['value']-=1000
        r=self.e.compute(at)['rebound']
        self.assertFalse(next(c['passed'] for c in r['conditions'] if c['name']=='비차익 순매도 둔화·순매수'))

    def test_stale_or_outside_session_never_confirms(self):
        self.run_series()
        s=self.e.compute(self.start+dt.timedelta(minutes=24))
        self.assertEqual(s['rebound']['stage'],0)
        self.assertIn('보류',s['rebound']['title'])
        s=self.e.compute(stamp('2026-10-30T16:00:00+09:00'))
        self.assertFalse(s['rebound']['active'])
        self.assertFalse(s['rebound']['confirmed'])

    def test_restart_does_not_mutate_last_historical_snapshot(self):
        self.run_series(15)
        self.e.store.close()
        self.e=Engine(config(),self.temp.name)
        at=self.start+dt.timedelta(minutes=15,seconds=30)
        self.e.new_day(at)
        old=copy.deepcopy(self.e.history[-1])
        self.e.ingest({'asof':at.isoformat(),'source':'test','values':{'foreign_fut_qty':100,'k200':502}},at)
        self.assertEqual(self.e.history[-1],old)
        self.assertIsNot(self.e.values,self.e.history[-1]['values'])

    def test_one_stale_cache_sample_is_not_a_long_market_gap(self):
        self.run_series(15)
        self.e.history[7]['values']['k200']['at']=(self.start+dt.timedelta(minutes=6)).isoformat()
        self.assertIsNotNone(self.e.delta('k200',15,self.start+dt.timedelta(minutes=15)))
        for h in self.e.history[6:11]:
            h['values'].pop('k200',None)
        self.assertIsNone(self.e.delta('k200',15,self.start+dt.timedelta(minutes=15)))

    def test_speed_uses_source_times_despite_delayed_cache(self):
        self.run_series(20)
        # A response for minute 5 was not cached until minute 6. It is known now.
        self.e.history[5]['values'].pop('nonarb_amt')
        self.e.history[6]['values']['nonarb_amt']['at']=(self.start+dt.timedelta(minutes=5)).isoformat()
        self.e.history[6]['values']['nonarb_amt']['value']=-350
        result=flow_speed(self.e,'nonarb_amt',self.start+dt.timedelta(minutes=20))
        self.assertTrue(result['available'])
        self.assertEqual(result['start_at'],(self.start+dt.timedelta(minutes=5)).isoformat())

    def test_regular_110_second_flow_updates_use_actual_elapsed_time(self):
        self.run_series(20)
        for h in self.e.history:
            seconds=int((stamp(h['at'])-self.start).total_seconds())//110*110
            h['values']['nonarb_amt']['at']=(self.start+dt.timedelta(seconds=seconds)).isoformat()
            h['values']['nonarb_amt']['value']=-seconds
        self.e.values['nonarb_amt']=dict(self.e.history[-1]['values']['nonarb_amt'])
        result=flow_speed(self.e,'nonarb_amt',self.start+dt.timedelta(minutes=20))
        self.assertTrue(result['available'])
        self.assertAlmostEqual(result['prior_rate'],-60)
        self.assertAlmostEqual(result['recent_rate'],-60)

    def test_real_gap_is_distinguished_from_current_receiving(self):
        self.run_series(20)
        self.e.history=self.e.history[:1]+self.e.history[15:]
        s=self.e.compute(self.start+dt.timedelta(minutes=20))
        row=next(d for d in s['rebound']['diagnostics'] if d['key']=='k200' and d['window']=='m15')
        self.assertIn('실제 관측 공백 10:00 → 10:15',row['reason'])
        self.assertIsNotNone(s['metrics']['k200']['m5'])


if __name__=='__main__':unittest.main()
