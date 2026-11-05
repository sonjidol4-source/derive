import json
import tempfile
import unittest
from pathlib import Path

import deriv_today as D
from common import config, stamp

CFG = config()

# 2026-10-22 마감 뒤 한투 실조회(백만원 원문) — 외국인·기관계·개인·금융투자
REAL_1022 = {
    'fut': [(1552, 313722), (-776, -147529), (-434, -93322), (3514, 772769)],
    'mini': [(-3606, -158909), (4056, 180715), (72, 1581), (4195, 187075)],
    'call': [(-3485, -2857), (738, 650), (2652, 2224), (736, 647)],
    'put': [(541, 9170), (-317, -9525), (-206, 261), (-313, -9738)],
    'wk_mon_call': [(7662, 29995), (-8512, -30937), (955, 956), (613, 3119)],
    'wk_mon_put': [(-750, -558), (106, 17), (672, 544), (106, 14)],
    'wk_thu_call': [(-5730, -4060), (-17180, -132), (22247, 4175), (-17371, -227)],
    'wk_thu_put': [(542, 1843), (-3657, 289), (2520, -2112), (-3468, 612)],
    'mini_call': [(-568, -466), (488, 484), (68, -54), (488, 484)],
    'mini_put': [(355, 812), (-308, -837), (-30, 24), (-308, -837)],
}
CODES = ['frgn', 'orgn', 'prsn', 'scrt']


def row(values):
    out = {}
    for code, (q, a) in zip(CODES, values):
        out[code + '_ntby_qty'] = str(q)
        out[code + '_ntby_tr_pbmn'] = str(a)
    return out


def record(at, data=REAL_1022, scale=1.0):
    return {'at': at, 'day': at[:10], 'source': 'test', 'errors': [],
            'markets': {k: D.parse_row(row([(q * scale, a * scale) for q, a in v])) for k, v in data.items()}}


class FakeRest:
    def __init__(self, fail=()):
        self.fail, self.calls = fail, []

    def get(self, path, tr_id, params):
        self.calls.append(params['FID_INPUT_ISCD_2'])
        key = next(k for k, _, _, iscd2, _, _ in D.MARKETS if iscd2 == params['FID_INPUT_ISCD_2'])
        if key in self.fail:
            raise ValueError('초당 거래건수 초과')
        return {'rt_cd': '0', 'output': [row(REAL_1022[key])]}


class FlowTests(unittest.TestCase):
    def test_units_and_mini_conversion(self):
        s = D.summarize(record('2026-10-22T21:54:00+09:00'))['foreign']
        self.assertEqual((s['fut'], s['mini'], s['fut_amt']), (1552, -3606, 3137.22))
        self.assertAlmostEqual(s['fut_eq'], 1552 - 0.2 * 3606)
        self.assertAlmostEqual(s['call']['total'], -28.57 + 299.95 - 40.60 - 4.66, places=2)
        self.assertAlmostEqual(s['put']['total'], 91.70 - 5.58 + 18.43 + 8.12, places=2)
        self.assertAlmostEqual(s['opt_bias'], s['call']['total'] - s['put']['total'])

    def test_readings(self):
        su = D.summarize(record('2026-10-22T21:54:00+09:00'))
        f = D.reading(su['foreign'])
        # 10/22 외국인: 콜 +226억·풋 +113억으로 둘 다 순매수 → 옵션 쪽 방향은 정하지 않고 선물 쪽만 남긴다.
        self.assertEqual(f['headline'], '선물 매수 우위 · 콜·풋 모두 순매수(콜이 더 큼) → 옵션 방향 판단 안 함')
        self.assertEqual(f['side'], 1)
        self.assertIn('선물 매수 우위 +831계약(일반 +1,552 · 미니 -3,606)', f['fut_text'])
        self.assertIn('옵션 콜·풋 모두 순매수(콜이 더 큼): 콜−풋 순매수대금 +113억', f['opt_text'])
        i = D.reading(su['inst'])
        self.assertEqual(i['headline'], '선물 뚜렷하지 않음 · 콜·풋 모두 순매도(콜이 더 큼) → 옵션 방향 판단 안 함')  # 선물 −776 + 미니 4,056×0.2 = +35
        p = D.reading(su['individual'])
        self.assertIn('엇갈림, 판단 보류', p['headline'])
        self.assertEqual(p['side'], 0)
        for r in (f, i, p):  # 대금 차이에 방향·전략 의미를 붙이지 않는다
            for word in ('상승 쪽', '하락 쪽', '큰 변동', '박스권'):
                self.assertNotIn(word, r['headline'] + r['opt_text'])

    def test_missing_weekly_call_withholds_option_reading(self):
        # GPT 검토 재현: 위클리(월) 콜만 빠지면 외국인 콜−풋이 +113억 → −186억으로 뒤집혀 보였다.
        rec = record('2026-10-22T21:54:00+09:00')
        del rec['markets']['wk_mon_call']
        s = D.summarize(rec)['foreign']
        self.assertIsNone(s['opt_bias'])
        self.assertIsNone(s['call']['total'])
        self.assertAlmostEqual(s['call']['partial_sum'], -28.57 - 40.60 - 4.66, places=2)
        r = D.reading(s)
        self.assertIn('옵션 판정 보류(위클리(월) 콜 값 없음', r['opt_text'])
        self.assertEqual(r['side'], 0)
        self.assertIn('옵션 자료 불완전', r['headline'])
        self.assertFalse(D.complete(rec))

    def test_option_word_names_the_leg(self):
        # 2026-10-23 15:50 외국인: 콜 −90억·풋 +14억 → 차이 −104억은 대부분 콜 순매도에서 나왔다.
        s = {'call': {'total': -90.0}, 'put': {'total': 14.0}, 'opt_bias': -104.0}
        self.assertEqual(D.option_word(s, 20), ('콜 순매도 주도', 87))
        s = {'call': {'total': -439.0}, 'put': {'total': -267.0}, 'opt_bias': -172.0}
        self.assertEqual(D.option_word(s, 20)[0], '콜·풋 모두 순매도(콜이 더 큼)')

    def test_momentum(self):
        ch = lambda m5, m15: {'m5': {'fut_eq': m5}, 'm15': {'fut_eq': m15}}
        self.assertEqual(D.momentum(2400, ch(10, 600), 1000)['state'], 2)
        self.assertEqual(D.momentum(2400, ch(-50, -700), 1000)['state'], -1)
        self.assertEqual(D.momentum(400, ch(-50, -700), 1000)['state'], -2)
        self.assertEqual(D.momentum(2400, ch(10, 100), 1000)['state'], 0)
        self.assertIn('신규·청산은 구분 불가', D.momentum(2400, ch(10, 600), 1000)['text'])

    def test_missing_mini_withholds_futures_total(self):
        rec = record('2026-10-22T21:54:00+09:00')
        del rec['markets']['mini']
        s = D.summarize(rec)['foreign']
        self.assertIsNone(s['fut_eq'])  # 미니를 0으로 두면 +831이 +1,552로 바뀌어 보인다
        r = D.reading(s)
        self.assertIn('선물 판정 보류(미니선물 값 없음 · 일반선물만 +1,552계약)', r['fut_text'])

    def test_empty_success_response_is_not_complete(self):
        class Empty(FakeRest):
            def get(self, path, tr_id, params):
                return {'rt_cd': '0', 'output': []}
        rec = D.poll(Empty(), clock=lambda: stamp('2026-10-23T16:20:00+09:00'))
        self.assertEqual(rec['markets'], {})
        self.assertEqual(len(rec['errors']), len(D.MARKETS))
        self.assertFalse(D.complete(rec))
        hollow = record('2026-10-23T16:20:30+09:00')
        hollow['markets']['mini_put'] = {k: {'qty': None, 'amt': None} for k in ('foreign', 'inst', 'individual', 'financial')}
        self.assertFalse(D.complete(hollow))  # 시장 이름만 있고 값이 빈 기록도 완료가 아니다
        with tempfile.TemporaryDirectory() as d:
            feed = D.DerivTodayFeed(CFG, None, None, directory=d)
            D.save(hollow, d)
            self.assertEqual(feed.due(stamp('2026-10-23T16:30:00+09:00')), 'final')

    def test_slow_poll_is_not_one_snapshot(self):
        rec = record('2026-10-22T21:54:40+09:00')
        rec['started'] = '2026-10-22T21:54:00+09:00'
        self.assertFalse(D.complete(rec))
        s = D.summarize(rec)['foreign']
        self.assertIsNone(s['fut_eq'])
        self.assertIsNone(s['opt_bias'])
        self.assertEqual(D.reading(s)['headline'], '자료 불완전 → 판단 보류')

    def test_flow_with_oi_never_claims_open_or_close(self):
        t = D.flow_with_oi(500, 800, '월물 콜')
        self.assertEqual(t, '월물 콜 외국인 매수 우위 · 상품 전체 미결제 증가 동반 (신규/청산 구분 불가)')
        self.assertNotIn('신규 매수', t)
        self.assertIsNone(D.flow_with_oi(20, 300, 'x'))

    def test_missing_market_is_not_zero(self):
        rec = D.poll(FakeRest(fail=('put',)), clock=lambda: stamp('2026-10-23T10:00:00+09:00'))
        self.assertEqual(len(rec['markets']), len(D.MARKETS) - 1)
        self.assertEqual(len(rec['errors']), 1)
        s = D.summarize(rec)['foreign']
        self.assertTrue(s['put']['partial'])
        self.assertIsNone(s['put']['total'])
        self.assertAlmostEqual(s['put']['partial_sum'], -5.58 + 18.43 + 8.12, places=2)

    def test_changes_and_view(self):
        with tempfile.TemporaryDirectory() as d:
            for hm, scale in (('09:30', 0.2), ('10:15', 0.6), ('10:30', 1.0)):
                D.save(record('2026-10-23T%s:00+09:00' % hm, scale=scale), d)
            v = D.view(d, CFG, at=stamp('2026-10-23T10:30:30+09:00'), spot=858.0)
            self.assertEqual(v['status'], 'live')
            f = v['investors'][0]
            self.assertEqual(f['key'], 'foreign')
            self.assertAlmostEqual(f['change']['m15']['fut_eq'], 830.8 * 0.4, places=1)
            self.assertAlmostEqual(f['change']['m60']['fut_eq'], 830.8 * 0.8, places=1)
            self.assertEqual([p['t'] for p in v['series']], ['09:30', '10:15', '10:30'])
            later = D.view(d, CFG, at=stamp('2026-10-23T18:00:00+09:00'), spot=858.0)
            self.assertEqual(later['status'], 'archive')


def snap(at, oi, month='202611'):
    calls = {'received_at': at, 'month': month, 'rows': [{'strike': k, 'cp': 'C', 'oi': v} for k, v in oi['C'].items()]}
    puts = {'received_at': at, 'month': month, 'rows': [{'strike': k, 'cp': 'P', 'oi': v} for k, v in oi['P'].items()]}
    return calls, puts


class OiTests(unittest.TestCase):
    def write(self, d, at, oi):
        c, p = snap(at, oi)
        (Path(d) / 'snapshot_calls.json').write_text(json.dumps({'snapshot': c}), encoding='utf-8')
        (Path(d) / 'snapshot_puts.json').write_text(json.dumps({'snapshot': p}), encoding='utf-8')
        D.track_oi(d)

    def test_prior_day_close_is_the_base(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, '2026-10-22T16:23:00+09:00', {'C': {870.0: 1000, 900.0: 1500, 1200.0: 9000}, 'P': {850.0: 2000, 830.0: 800}})
            first = D.oi_view(d, 858.0)
            self.assertTrue(first['same_snapshot'])
            self.assertEqual(first['headline'], '아직 비교할 변화가 없습니다')
            self.write(d, '2026-10-23T10:00:00+09:00', {'C': {870.0: 1600, 900.0: 1400, 1200.0: 9900}, 'P': {850.0: 2300, 830.0: 800}})
            v = D.oi_view(d, 858.0)
            self.assertIn('10/22 마지막 조회(16:23) 대비', v['base'])
            self.assertEqual([(r['strike'], r['chg']) for r in v['call_up']], [(870.0, 600)])  # 1200은 지수 ±7% 밖
            self.assertEqual([(r['strike'], r['chg']) for r in v['call_down']], [(900.0, -100)])
            self.assertEqual([(r['strike'], r['chg']) for r in v['put_up']], [(850.0, 300)])
            self.assertIn('870(+600)', v['headline'])
            self.assertNotIn('저항', v['headline'])

    def test_itm_strikes_are_not_called_walls(self):
        # GPT 검토 재현: 지수 858에서 콜 850·풋 870 미결제가 늘면 '위쪽 저항/아래쪽 지지'라고 잘못 썼다.
        with tempfile.TemporaryDirectory() as d:
            self.write(d, '2026-10-22T16:23:00+09:00', {'C': {850.0: 1000, 880.0: 100}, 'P': {870.0: 1000, 840.0: 100}})
            self.write(d, '2026-10-23T10:00:00+09:00', {'C': {850.0: 1500, 880.0: 150}, 'P': {870.0: 1500, 840.0: 120}})
            v = D.oi_view(d, 858.0)
            self.assertEqual([r['strike'] for r in v['call_up']], [880.0])
            self.assertEqual([r['strike'] for r in v['put_up']], [840.0])
            self.assertIn('지수 위 콜 미결제 증가 최대 880(+50)', v['headline'])
            self.assertIn('지수 아래 풋 미결제 증가 최대 840(+20)', v['headline'])
            self.assertEqual(D.oi_view(d, None)['headline'], '지수 값이 없어 행사가의 위·아래를 구분하지 못했습니다')

    def test_same_snapshot_is_recorded_once(self):
        with tempfile.TemporaryDirectory() as d:
            for _ in range(2):
                self.write(d, '2026-10-23T10:00:00+09:00', {'C': {870.0: 1}, 'P': {850.0: 1}})
            state = json.loads((Path(d) / D.OI_FILE).read_text(encoding='utf-8'))
            self.assertEqual(state['days']['2026-10-23']['first']['at'], state['days']['2026-10-23']['last']['at'])


class FeedTimingTests(unittest.TestCase):
    def test_due(self):
        with tempfile.TemporaryDirectory() as d:
            feed = D.DerivTodayFeed(CFG, None, None, directory=d)
            self.assertEqual(feed.due(stamp('2026-10-23T08:40:00+09:00')), None)
            self.assertEqual(feed.due(stamp('2026-10-23T08:45:00+09:00')), 'session')  # 선물 정규장 08:45 개장
            self.assertEqual(feed.due(stamp('2026-10-23T09:00:00+09:00')), 'session')
            self.assertEqual(feed.due(stamp('2026-10-23T16:00:00+09:00')), None)
            self.assertEqual(feed.due(stamp('2026-10-23T16:20:00+09:00')), 'final')
            broken = record('2026-10-23T16:20:30+09:00')
            del broken['markets']['mini_put']
            D.save(broken, d)
            self.assertEqual(feed.due(stamp('2026-10-23T16:30:00+09:00')), 'final')  # 일부 실패한 마감 조회는 완료가 아니다
            D.save(record('2026-10-23T16:31:30+09:00'), d)
            self.assertEqual(feed.due(stamp('2026-10-23T16:40:00+09:00')), None)
            self.assertEqual(feed.due(stamp('2026-10-24T18:30:00+09:00')), None)
            self.assertEqual(feed.due(stamp('2026-10-24T10:00:00+09:00')), None)  # 토요일


if __name__ == '__main__':
    unittest.main()
