import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from common import stamp
import kis_gap as K

DAY = dt.date(2026, 11, 5)


def rec(hm):
    return {'at': '2026-11-05T%s:20+09:00' % hm, 'markets': {'fut': {'foreign': {'qty': -1}}}}


def minutes(a, b):
    t = dt.datetime.combine(DAY, dt.time.fromisoformat(a))
    while t.strftime('%H:%M') <= b:
        yield t.strftime('%H:%M')
        t += dt.timedelta(minutes=1)


class FakeLs:
    """t1617 흉내: cts_time 이전 20행(30초 간격)을 최신 먼저. 값은 시장별 고정 패턴."""
    BASE = {'3': -1400, '8': -2900, '4': -35, '5': 8, '9': -1, '0': 0}

    def __init__(self):
        self.calls = []

    def __call__(self, path, tr, block, cont):
        self.calls.append(block)
        cts = block['cts_time']
        t = dt.datetime.combine(DAY, dt.time(int(cts[:2]), int(cts[2:4]), int(cts[4:6])))
        rows = []
        for _ in range(20):
            t -= dt.timedelta(seconds=30)
            if t.strftime('%H:%M') < '08:45':
                break
            v = self.BASE[block['gubun1']] + t.minute
            rows.append({'time': t.strftime('%H%M%S') + '00', 'sv_17': v, 'sv_18': -v, 'sv_08': 0, 'sv_01': 0})
        nxt = rows[-1]['time'] if rows else ''
        return {'rsp_cd': '00000', 't1617OutBlock': {'cts_time': nxt}, 't1617OutBlock1': rows}, 'Y'


class KisGapTests(unittest.TestCase):
    def test_find_gaps_ignores_single_minutes_and_live_edge(self):
        records = [rec(m) for m in minutes('09:26', '12:05')] + [rec(m) for m in minutes('12:34', '12:40')]
        records = [r for r in records if r['at'][11:16] != '10:00']  # 한 분 빠짐은 공백으로 치지 않는다
        gaps = K.find_gaps(records, DAY, stamp('2026-11-05T12:41:00+09:00'))
        self.assertEqual(gaps, [('08:46', '09:25'), ('12:06', '12:33')])

    def test_recover_fills_same_basis_and_skips_done(self):
        records = [rec(m) for m in minutes('08:46', '12:05')] + [rec(m) for m in minutes('12:34', '12:50')]
        ls = FakeLs()
        with tempfile.TemporaryDirectory() as d:
            r = K.recover(ls, d, DAY, records, stamp('2026-11-05T12:52:00+09:00'))
            self.assertEqual((r['gaps'], r['fetched'], r['pending']), ([('12:06', '12:33')], 6, 0))
            # 12:34(끝 다음 분)부터 거꾸로, 30분 공백이라 시장당 3쪽
            self.assertEqual(ls.calls[0]['cts_time'], '12340000'); self.assertEqual(len(ls.calls), 18)
            again = K.recover(ls, d, DAY, records, stamp('2026-11-05T12:53:00+09:00'))
            self.assertEqual(again['fetched'], 0); self.assertEqual(len(ls.calls), 18)
            v = K.view(d, DAY, records)
            pts = {p['t']: p['foreign'] for p in v['points']}
            self.assertEqual(sorted(pts)[0], '12:06'); self.assertEqual(sorted(pts)[-1], '12:33'); self.assertEqual(len(pts), 28)
            f = pts['12:10']
            self.assertEqual((f['fut'], f['mini'], f['fut_eq']), (-1390, -2890, round(-1390 + 0.2 * -2890, 1)))
            self.assertEqual(f['opt_partial'], (-35 + 10) + (-1 + 10) - (8 + 10) - (0 + 10))

    def test_partial_progress_is_kept_when_limit_hits(self):
        records = [rec(m) for m in minutes('08:46', '12:05')] + [rec(m) for m in minutes('12:34', '12:50')]
        ls = FakeLs()
        def limited(*a):
            if len(ls.calls) >= 7:
                raise ValueError('복구 조회 한도(48회·3분)에 도달했습니다.')
            return ls(*a)
        with tempfile.TemporaryDirectory() as d:
            r = K.recover(limited, d, DAY, records, stamp('2026-11-05T12:52:00+09:00'))
            self.assertEqual((r['fetched'], r['pending']), (2, 4)); self.assertIn('한도', r['error'])
            r2 = K.recover(ls, d, DAY, records, stamp('2026-11-05T12:53:00+09:00'))
            self.assertEqual((r2['fetched'], r2['pending']), (4, 0))

    def test_other_day_state_is_reset(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / K.FILE).write_text(json.dumps({'day': '2026-11-04', 'rows': {}, 'covered': {'fut': [['12:00', '13:00']]}}), encoding='utf-8')
            self.assertEqual(K.load(d, DAY)['covered']['fut'], [])

    def test_lsfault_raises(self):
        with self.assertRaises(ValueError):
            K.fetch(lambda *a: ({'rsp_cd': 'IGW00201', 'rsp_msg': '초과'}, 'N'), 'fut', '12:06', '12:33')


if __name__ == '__main__':
    unittest.main()
