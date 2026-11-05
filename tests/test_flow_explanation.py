import datetime as dt
import tempfile
import unittest
from common import config, stamp
from engine import Engine
from flow_explanation import flow_speed


class FlowExplanationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.e = Engine(config(), self.temp.name)
        self.start = stamp('2026-10-30T10:00:00+09:00')

    def tearDown(self):
        self.e.store.close()
        self.temp.cleanup()

    def fill(self, foreign5=224, cash='nonarb_amt', changed_source=False, gap=False):
        for i in range(16):
            if gap and 5 <= i <= 9:
                continue
            at = self.start + dt.timedelta(minutes=i)
            values = {'k200': 500+i*.12, 'foreign_fut_qty': -1000+i*foreign5/5,
                      'foreign_spot_amt': -5000-i*30, 'fut_price': 501+i*.12,
                      'fut_prev_close': 505, 'basis': -.8+i*.01, 'basis_theory': .2,
                      'fut_oi': 100000+i*16, 'breadth': 30}
            if cash != 'foreign_spot_amt':
                values[cash] = -67.3*min(i, 10)-34*max(i-10, 0)
            self.e.ingest({'asof': at.isoformat(), 'source': 'other' if changed_source and i == 7 else 'test',
                           'scope': 'cumulative_day', 'fut_code': 'F1', 'values': values}, at)
            result = self.e.compute(at)
        return result

    def test_positive_flow_below_threshold_and_day_separate(self):
        s = self.fill()
        x = s['flow_explanation']
        self.assertEqual(s['verdict']['label'], '혼조 / 변화 관찰')
        self.assertEqual(x['m5']['title'], '동조 조건 미충족')
        axis = x['m5']['axes'][0]
        self.assertEqual((axis['value'], axis['threshold'], axis['direction'], axis['state']), (224, 300, 0, '기준 미달'))
        self.assertIn('방향 엇갈림', x['m5']['summary'])
        self.assertLess(x['day'][0]['value'], 0)
        self.assertGreater(x['m15'][-1]['value'], 0)
        self.assertTrue(all('선물' in r['name'] or '현물' in r['name'] or '종목' in r['name'] for r in x['day']))

    def test_negative_flow_can_slow_down(self):
        s = self.fill()
        speed = s['flow_explanation']['speeds'][0]
        self.assertTrue(speed['available'])
        self.assertAlmostEqual(speed['prior'], -673)
        self.assertAlmostEqual(speed['recent'], -170)
        self.assertAlmostEqual(speed['prior_rate'], -67.3)
        self.assertAlmostEqual(speed['recent_rate'], -34)
        self.assertEqual(speed['text'], '순매도 지속 · 순매도 속도 완화')
        self.assertEqual(speed['boundary_at'], '2026-10-30T10:10:00+09:00')

    def test_exact_thresholds_match_verdict_axes(self):
        for value, expected in ((300, 1), (-300, -1)):
            with self.subTest(value=value):
                self.e.values.clear()
                self.e.history.clear()
                self.e.last_tick_minute = None
                s = self.fill(foreign5=value)
                self.assertEqual(s['flow_explanation']['m5']['axes'][0]['direction'], expected)
                self.assertEqual(s['verdict']['label'], '현물·선물 엇갈림' if expected == 1 else '혼조 / 변화 관찰')

    def test_fallback_names_and_units(self):
        for key, name in (('program_amt', '전체 프로그램'), ('foreign_spot_amt', '외국인 현물')):
            with self.subTest(key=key):
                with tempfile.TemporaryDirectory() as directory:
                    original = self.e
                    self.e = Engine(config(), directory)
                    try:
                        s = self.fill(cash=key)
                        self.assertEqual(s['flow_explanation']['m5']['axes'][1]['name'], name)
                        self.assertEqual(s['flow_explanation']['m5']['axes'][1]['threshold'], 100)
                        self.assertTrue(any(t.startswith(name+' 최근 5분') for t in s['verdict']['facts']))
                    finally:
                        self.e.store.close()
                        self.e = original

    def test_source_change_and_gap_block_rate_comparison(self):
        for kwargs in ({'changed_source': True}, {'gap': True}):
            with self.subTest(kwargs=kwargs):
                self.e.values.clear()
                self.e.history.clear()
                self.e.last_tick_minute = None
                s = self.fill(**kwargs)
                self.assertFalse(s['flow_explanation']['speeds'][0]['available'])

    def test_scope_change_and_manual_data_block_comparison(self):
        self.fill()
        for field, value in (('scope', 'different_market'), ('quality', '사용자 입력')):
            row = self.e.history[7]['values']['nonarb_amt']
            original = row.get(field)
            row[field] = value
            self.assertFalse(flow_speed(self.e, 'nonarb_amt', self.start+dt.timedelta(minutes=15))['available'])
            row[field] = original

    def test_stale_missing_and_new_day_are_not_neutral_rates(self):
        self.fill()
        for at in (self.start+dt.timedelta(minutes=20), self.start+dt.timedelta(days=1)):
            s = self.e.compute(at)
            self.assertTrue(all(not row['available'] for row in s['flow_explanation']['speeds']))
            self.assertTrue(all(a['value'] is None for a in s['flow_explanation']['m5']['axes']))

    def test_rate_uses_actual_source_timestamps(self):
        self.fill()
        # Same values, last source observation 30 seconds after the stored snapshot.
        at = self.start+dt.timedelta(minutes=15, seconds=30)
        self.e.ingest({'asof': at.isoformat(), 'source': 'test', 'scope': 'cumulative_day',
                       'values': {'nonarb_amt': -843}}, at)
        speed = flow_speed(self.e, 'nonarb_amt', at)
        self.assertTrue(speed['available'])
        self.assertAlmostEqual(speed['recent_minutes'], 5.5)
        self.assertAlmostEqual(speed['recent_rate'], -170/5.5)

    def test_stale_boundary_even_with_fresh_snapshot_is_rejected(self):
        self.fill()
        self.e.history[10]['values']['nonarb_amt']['at'] = (self.start+dt.timedelta(minutes=8)).isoformat()
        speed = flow_speed(self.e, 'nonarb_amt', self.start+dt.timedelta(minutes=15))
        self.assertFalse(speed['available'])


if __name__ == '__main__':
    unittest.main()
