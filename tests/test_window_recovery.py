"""The four user-requested acceptance scenarios; no network or Windows sleep."""
import datetime as dt
import tempfile
import unittest
from common import config, stamp
from engine import Engine


class WindowRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.e=Engine(config(),self.temp.name)
        self.start=stamp('2026-10-30T10:00:00+09:00')

    def tearDown(self):
        self.e.store.close();self.temp.cleanup()

    def packet(self,minute,clock=None,source='LS t2111'):
        at=clock or self.start+dt.timedelta(minutes=minute,seconds=4)
        values={'k200':500+minute*.12,'foreign_fut_qty':minute*50,
                'nonarb_amt':-70*min(minute,56)-20*max(minute-56,0),
                'foreign_spot_amt':-30*min(minute,56)-10*max(minute-56,0),
                'basis':1+minute*.02,'basis_theory':1.2}
        self.e.ingest({'asof':at.isoformat(),'source':source,'scope':'market','fut_code':'F1','values':values},at)
        return at

    def baseline_case(self,all_invalid=False,missing=False):
        for i in range(11):
            clock=self.start+dt.timedelta(minutes=i,seconds=4)
            if i==4 and not all_invalid:
                self.packet(i,self.start+dt.timedelta(minutes=3,seconds=54))
            elif i not in ((4,5) if all_invalid else (5,)):
                self.packet(i,clock)
            if i==5 and missing:
                self.e.values.pop('k200',None)
            self.e.compute(clock)
        at=self.start+dt.timedelta(minutes=10,seconds=5)
        return self.e.compute(at)

    def test_1_invalid_nearest_record_uses_earlier_valid_record(self):
        s=self.baseline_case()
        self.assertAlmostEqual(s['metrics']['k200']['m5'],.72)
        self.assertAlmostEqual(s['metrics']['basis_residual']['m5'],.12)
        inputs=self.e.observation_inputs(stamp(s['generated_at']),['k200','basis_residual'],[])
        bases=[o for o in inputs if o.get('role')=='window_base' and o.get('field') in ('k200','basis_residual')]
        self.assertTrue(bases)
        self.assertTrue(all(o['history_at'].startswith('2026-10-30T10:04:04') for o in bases))

    def test_1b_missing_nearest_value_uses_earlier_valid_record(self):
        s=self.baseline_case(missing=True)
        self.assertAlmostEqual(s['metrics']['k200']['m5'],.72)
        self.assertAlmostEqual(s['metrics']['basis_residual']['m5'],.12)

    def test_2_all_candidates_invalid_remains_missing(self):
        s=self.baseline_case(all_invalid=True)
        self.assertIsNone(s['metrics']['k200']['m5'])
        self.assertIsNone(s['metrics']['basis_residual']['m5'])
        condition=next(c for c in s['rebound']['conditions'] if c['name']=='KOSPI200 5분 상승')
        self.assertIsNone(condition['passed'])
        self.assertFalse(s['rebound']['confirmed'])

    def test_3_source_switch_in_recorded_window_is_blocked(self):
        for i in range(11):
            at=self.packet(i,source='LS t2111' if i<7 else 'REST ka20001')
            s=self.e.compute(at)
        self.assertIsNone(s['metrics']['k200']['m5'])
        self.assertFalse(s['rebound']['confirmed'])

    def test_3b_source_switch_after_latest_snapshot_is_also_blocked(self):
        for i in range(11):
            self.e.compute(self.packet(i))
        at=self.start+dt.timedelta(minutes=10,seconds=50)
        self.e.ingest({'asof':at.isoformat(),'source':'REST ka20001','scope':'market','values':{'k200':900}},at)
        self.assertEqual(self.e.values['k200']['source'],'REST ka20001')
        self.assertIsNone(self.e.delta('k200',5,at))

    def test_3c_stable_new_source_recovers_without_mixing_old_source(self):
        for i in range(23):
            at=self.packet(i,source='LS t2111' if i<7 else 'REST ka20001')
            s=self.e.compute(at)
            if i==11:self.assertIsNone(s['metrics']['k200']['m5'])
            if i==12:
                self.assertIsNotNone(s['metrics']['k200']['m5'])
                self.assertIsNone(s['metrics']['k200']['m15'])
        self.assertIsNotNone(s['metrics']['k200']['m15'])

    def test_4_sleep_gap_then_independent_five_and_fifteen_minute_recovery(self):
        for i in range(11):
            self.e.compute(self.packet(i))
        results={}
        for i in range(46,65):
            results[i-46]=self.e.compute(self.packet(i))
        for elapsed in (0,4):
            s=results[elapsed]
            for key in ('k200','basis_residual','foreign_fut_qty','nonarb_amt'):
                self.assertIsNone(s['metrics'][key]['m5'])
                self.assertIsNone(s['metrics'][key]['m15'])
        for elapsed in (5,9,14):
            s=results[elapsed]
            for key in ('k200','basis_residual','foreign_fut_qty','nonarb_amt'):
                self.assertIsNotNone(s['metrics'][key]['m5'])
                self.assertIsNone(s['metrics'][key]['m15'])
            self.assertLess(s['rebound']['stage'],2)
            self.assertFalse(s['rebound']['confirmed'])
        for key in ('k200','basis_residual','foreign_fut_qty','nonarb_amt'):
            self.assertIsNotNone(results[15]['metrics'][key]['m15'])
        self.assertFalse(results[15]['rebound']['confirmed'])
        self.assertTrue(results[18]['rebound']['confirmed'])
        self.assertTrue(all(e['at']>='2026-10-30T11:04' for e in self.e.store.events() if e['kind'].startswith('반등')))


if __name__=='__main__':unittest.main()
