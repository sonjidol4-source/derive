import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path

from common import config, stamp
import stock_futures as sf

CFG = config()
MASTER = [  # 2026-11-04 t8401 응답 형식(일부)
    {'hname': '삼성전자   F 202612', 'shcode': 'A116C000', 'basecode': 'A005930'},
    {'hname': '삼성전자   F 202611', 'shcode': 'A116B000', 'basecode': 'A005930'},
    {'hname': '삼성전자   F 202610', 'shcode': 'A116A000', 'basecode': 'A005930'},
    {'hname': '삼성전자   SP 2611-2612', 'shcode': 'A416B000', 'basecode': 'A005930'},
    {'hname': 'SK하이닉스 F 202611', 'shcode': 'A506B000', 'basecode': 'A000660'},
    {'hname': 'TKG휴켐스  F 202611', 'shcode': 'A0A6B000', 'basecode': 'A069260'},
]
SAMSUNG = {'rsp_cd': '00000', 't8402OutBlock': {  # 2026-11-04 장 마감 뒤 실제 값
    'hname': '삼성전자   F 202611', 'price': 219000, 'diff': '-0.45', 'volume': 3419450, 'value': 7393586680, 'mgjv': 4271494,
    'mgjvdiff': 1904, 'sbasis': 2000, 'ibasis': -833, 'theoryprice': 218167, 'lastmonth': '20261112', 'jandatecnt': 9,
    'baseprice': 217000, 'basediff': '-1.14', 'basefornetbid': -1867581}}


class StockFuturesTests(unittest.TestCase):
    def test_front_month_skips_past_months_and_spreads(self):
        m = sf.front_months(MASTER, ['005930', '000660', '035720'], dt.date(2026, 11, 4))
        self.assertEqual([x[1] for x in m['005930']], ['A116B000', 'A116C000'])
        self.assertEqual(m['000660'][0][1], 'A506B000')
        self.assertNotIn('035720', m)

    def test_parse_real_response(self):
        r = sf.parse(SAMSUNG, '005930', '삼성전자', '202611', stamp('2026-11-04T16:30:00+09:00'))
        self.assertEqual((r['price'], r['base_price'], r['oi'], r['oi_change']), (219000, 217000, 4271494, 1904))
        self.assertEqual((r['basis_gap'], r['basis_gap_pct'], r['value_eok']), (2833, 1.31, 73936))
        self.assertEqual((r['foreign_member_net'], r['expiry'], r['days_left']), (-1867581, '20261112', 9))

    def test_bad_response(self):
        with self.assertRaises(ValueError):
            sf.parse({'rsp_cd': 'IGW00201', 'rsp_msg': '호출 거래건수를 초과하였습니다.'}, '005930', '삼성전자', '202611', stamp('2026-11-04T10:00:00+09:00'))
        with self.assertRaises(ValueError):
            sf.parse({'rsp_cd': '00000', 't8402OutBlock': {}}, '005930', '삼성전자', '202611', stamp('2026-11-04T10:00:00+09:00'))

    def test_view_modes(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sf.view(Path(d), CFG)['status'], 'empty')
            row = sf.parse(SAMSUNG, '005930', '삼성전자', '202611', stamp('2026-11-04T10:00:00+09:00'))
            sf.save([row], stamp('2026-11-04T10:00:00+09:00'), [], Path(d))
            self.assertEqual(sf.view(Path(d), CFG, stamp('2026-11-04T10:02:00+09:00'))['mode'], 'live')
            self.assertEqual(sf.view(Path(d), CFG, stamp('2026-11-04T10:30:00+09:00'))['mode'], 'stale')
            self.assertEqual(sf.view(Path(d), CFG, stamp('2026-11-04T17:00:00+09:00'))['mode'], 'archive')


class FeedTests(unittest.TestCase):
    """LsFeed.stock_futures: 마스터는 하루 한 번, 만기 지난 월물은 다음 월물로."""
    def test_feed_rolls_after_expiry_and_saves(self):
        from ls_feed import LsFeed
        import ls_feed
        with tempfile.TemporaryDirectory() as d:
            class E:
                lock = threading.RLock(); connections = {}
                store = type('S', (), {'directory': Path(d)})()
                def connection(self, n, s, detail=''): self.connections[n] = (s, detail)
            feed = LsFeed({**CFG, 'rest': {**CFG['rest'], 'watchlist': [{'code': '005930', 'name': '삼성전자'}]}}, E(), threading.Event())
            calls = []
            expired = {'rsp_cd': '00000', 't8402OutBlock': {**SAMSUNG['t8402OutBlock'], 'lastmonth': '20261112'}}
            nxt = {'rsp_cd': '00000', 't8402OutBlock': {**SAMSUNG['t8402OutBlock'], 'lastmonth': '20261210', 'price': 220000}}
            def call(req, path, tr, block):
                calls.append((tr, block))
                if tr == 't8401': return {'rsp_cd': '00000', 't8401OutBlock': MASTER}
                return expired if block['focode'] == 'A116B000' else nxt
            feed.call = call
            orig = ls_feed.now_kst
            ls_feed.now_kst = lambda: stamp('2026-11-12T16:00:00+09:00')  # 11월물 만기일 장 마감 뒤
            self.addCleanup(setattr, ls_feed, 'now_kst', orig)
            import requests
            self.assertTrue(feed.stock_futures(requests))
            self.assertTrue(feed.stock_futures(requests))
            self.assertEqual([c[0] for c in calls].count('t8401'), 1)
            data = sf.view(Path(d), CFG, stamp('2026-11-12T16:01:00+09:00'))
            self.assertEqual((data['rows'][0]['month'], data['rows'][0]['price']), ('202612', 220000))


if __name__ == '__main__':
    unittest.main()
