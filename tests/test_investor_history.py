import json
import tempfile
import unittest
from pathlib import Path

import deriv_today as D
import investor_history as H
from common import config, stamp
from engine import Engine

CFG = config()
FIX = json.loads((Path(__file__).parent / 'fixtures' / 't1665_20261022.json').read_text(encoding='utf-8'))


def fake_call(path, tr, block):
    key = 't1665_%s_1_1' % block['market']
    body = dict(FIX[key])
    rows = [r for r in body['t1665OutBlock1'] if block['from_date'] <= r['date'] <= block['to_date']]
    body['t1665OutBlock1'] = rows
    return body


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # 정리는 뒤에 등록한 DB 닫기부터(역순) → 폴더 삭제
        self.d = Path(self.tmp.name)
        self.state = H.save(self.d, H.fetch(fake_call, start='20260701', end='20261022', wait=lambda: None), '2026-10-23T08:30:00+09:00')


    def test_confirmed_values(self):
        day = self.state['days']['2026-10-22']
        self.assertEqual(day['fut']['qty'], {'foreign': 1552, 'inst': -776, 'individual': -434, 'financial': 3514})
        self.assertEqual(day['fut']['amt']['foreign'], 3137)
        self.assertEqual(day['call']['qty']['foreign'], -3485)   # 시장6 = 콜
        self.assertEqual(day['put']['qty']['foreign'], 541)      # 시장5 = 풋
        self.assertEqual(self.state['days']['2026-10-21']['fut']['qty']['foreign'], -3632)
        self.assertEqual(len(self.state['days']), 76)

    def test_cumulative_since_expiry(self):
        c = H.cumulative(self.state, CFG, stamp('2026-10-23T10:00:00+09:00'))
        self.assertEqual(c['since'], '2026-09-10')
        f = c['investors']['foreign']
        expect = sum(v for d, v in H.series(self.state, 'fut', 'foreign') if d > '2026-09-10')
        self.assertEqual(f['since_expiry'], expect)
        self.assertEqual(f['last'], 1552)
        self.assertTrue(0 <= f['last20_pct'] <= 100)
        self.assertEqual(c['missing'], [])

    def test_engine_cumulative_prefers_confirmed(self):
        e = Engine(CFG, self.d)
        self.addCleanup(e.store.close)  # Windows에서 열린 DB가 임시 폴더 삭제를 막지 않게
        e.store.save_daily('2026-10-22', {'foreign_fut_qty': -256, 'quality': '장중 잠정'})
        c = e.cumulative(stamp('2026-10-23T10:00:00+09:00'))
        expect = sum(v for d, v in H.series(self.state, 'fut', 'foreign') if d > '2026-09-10')
        self.assertEqual(c['sum'], expect)
        self.assertEqual(c['provisional'], [])

    def test_confirm_daily_keeps_intraday_copy(self):
        e = Engine(CFG, self.d)
        self.addCleanup(e.store.close)  # Windows에서 열린 DB가 임시 폴더 삭제를 막지 않게
        e.store.save_daily('2026-10-22', {'foreign_fut_qty': -256, 'inst_fut_qty': 2340, 'quality': '장중 잠정', 'finalized': False})
        self.assertEqual(e.confirm_daily(self.state), ['2026-10-22'])
        row = e.store.daily_row('2026-10-22')
        self.assertEqual((row['foreign_fut_qty'], row['inst_fut_qty'], row['finalized']), (1552, -776, True))
        self.assertEqual((row['foreign_fut_qty_intraday'], row['inst_fut_qty_intraday']), (-256, 2340))
        self.assertEqual(e.confirm_daily(self.state), [])  # 다시 돌려도 장중 사본을 덮지 않는다
        self.assertEqual(e.store.daily_row('2026-10-22')['foreign_fut_qty_intraday'], -256)
        csv = (self.d / 'daily.csv').read_text(encoding='utf-8-sig')
        self.assertIn('1552', csv)

    def test_unsettled_today_is_not_saved_as_confirmed(self):
        days = {'2026-10-22': {}, '2026-10-23': {}}
        self.assertEqual(list(H.settled(days, stamp('2026-10-23T11:00:00+09:00'), CFG)), ['2026-10-22'])
        self.assertEqual(list(H.settled(days, stamp('2026-10-23T16:30:00+09:00'), CFG)), ['2026-10-22', '2026-10-23'])

    def test_close_summary_cross_check(self):
        import close_summary as cs
        rec = {'day': '2026-10-22', 'status': '확정 추정', 'last_change': '16:07:00', 'qty': {'17': 1552, '18': -776, '08': -434},
               'amt': {'17': 3137}}
        (self.d / cs.FILE).write_text(json.dumps({'days': {'2026-10-22': rec}}), encoding='utf-8')
        v = cs.view(self.d, cfg=CFG)
        self.assertTrue(v['rows'][0]['cross']['match'])
        rec['qty']['17'] = -256
        (self.d / cs.FILE).write_text(json.dumps({'days': {'2026-10-22': rec}}), encoding='utf-8')
        v = cs.view(self.d, cfg=CFG)
        self.assertFalse(v['rows'][0]['cross']['match'])
        self.assertIn('LS 일별 확정 이력(t1665)과 다릅니다', v['latest']['notes'][0])

    def test_rank_does_not_look_ahead(self):
        all_ = H.daily_rank(self.state, 'fut', 'foreign', 0)
        past = H.daily_rank(self.state, 'fut', 'foreign', 0, before='2026-08-01')
        self.assertLess(past['n'], all_['n'])
        n_before = len([d for d, _ in H.series(self.state, 'fut', 'foreign') if d < '2026-08-01'])
        self.assertEqual(past['n'], min(60, n_before))

    def test_same_time_values_ignore_later_days(self):
        for day in ('2026-10-20', '2026-10-21', '2026-10-23'):
            D.save({'at': day + 'T10:00:00+09:00', 'day': day, 'markets': {'fut': {'foreign': {'qty': 100.0, 'amt': 1.0}},
                                                                            'mini': {'foreign': {'qty': 0.0, 'amt': 0.0}}}}, self.d)
        vals = D.same_time_values(self.d, 'foreign', 'fut_eq', stamp('2026-10-21T10:00:00+09:00'), exclude='2026-10-21')
        self.assertEqual(len(vals), 1)  # 10/20만. 10/23(이후 날짜)은 쓰지 않는다

    def test_cumulative_today_not_missing(self):
        e = Engine(CFG, self.d)
        self.addCleanup(e.store.close)  # Windows에서 열린 DB가 임시 폴더 삭제를 막지 않게
        c = e.cumulative(stamp('2026-10-23T10:00:00+09:00'))
        self.assertEqual(c['missing'], [])
        self.assertTrue(c['complete'])

    def test_threshold_from_history(self):
        th = D.thresholds(self.d, self.state, 'foreign', stamp('2026-10-23T12:15:00+09:00'), today='2026-10-23')
        self.assertIn('마감 |순매수| 중앙값', th['fut_eq']['basis'])
        self.assertIn('고정 초기값', th['opt_bias']['basis'])
        early = D.thresholds(self.d, self.state, 'foreign', stamp('2026-10-23T09:00:00+09:00'), today='2026-10-23')
        self.assertLess(early['fut_eq']['flat'], th['fut_eq']['flat'])


class EstimateTests(unittest.TestCase):
    def test_flow_with_oi_words(self):
        self.assertIn('매도 우위 · 상품 전체 미결제 감소 동반 (신규/청산 구분 불가)', D.flow_with_oi(-500, -300, '월물 풋'))

    def test_metrics(self):
        calls = [{'strike': k, 'cp': 'C', 'oi': 100, 'iv': 30 - (k - 850) * 0.05} for k in range(800, 905, 5)]
        puts = [{'strike': k, 'cp': 'P', 'oi': 120, 'iv': 30 + (850 - k) * 0.1} for k in range(800, 905, 5)]
        m = D.option_metrics(calls, puts, 850.0)
        self.assertEqual(m['pcr_oi'], 1.2)
        # 기준 행사가에 가까운 4개(콜·풋 각각)의 중앙값
        self.assertAlmostEqual(m['atm_iv'], (30.125 + 30.25) / 2, places=2)
        self.assertEqual(m['skew_strikes'], [[800, 805, 810, 815], [885, 890, 895, 900]])  # 지수 −5%(807.5)·+5%(892.5) 주변
        self.assertAlmostEqual(m['skew'], 34.25 - 27.875, places=1)
        self.assertEqual(m['method'], D.METRIC_METHOD)

    def test_metrics_ignore_illiquid_strikes(self):
        # 2026-10-29 장중처럼 거래 드문 2.5 간격 행사가가 오래된 가격으로 터무니없는 IV를 주어도 값이 흔들리지 않아야 한다.
        calls = [{'strike': k, 'cp': 'C', 'oi': 500, 'iv': 49.0} for k in range(850, 1055, 5)]
        puts = [{'strike': k, 'cp': 'P', 'oi': 500, 'iv': 50.0} for k in range(850, 1055, 5)]
        clean = D.option_metrics(calls, puts, 935.87)
        noisy_c = calls + [{'strike': k + 2.5, 'cp': 'C', 'oi': 30, 'iv': 65.0} for k in range(850, 1050, 5)]
        noisy_p = puts + [{'strike': k + 2.5, 'cp': 'P', 'oi': 20, 'iv': 36.0} for k in range(850, 1050, 5)]             + [{'strike': 887.5, 'cp': 'P', 'oi': 900, 'iv': 0.01}]
        noisy = D.option_metrics(noisy_c, noisy_p, 935.87)
        self.assertEqual((noisy['atm_iv'], noisy['skew']), (clean['atm_iv'], clean['skew']))
        self.assertEqual((clean['atm_iv'], clean['skew']), (49.5, 1.0))


if __name__ == '__main__':
    unittest.main()
