import tempfile
import unittest
from common import config,stamp
from engine import Engine
from quote_snapshot import futures_snapshot,options_snapshot


class QuoteSnapshotTests(unittest.TestCase):
    def test_startup_futures_response_contains_reference_without_live_tick(self):
        result={'선옵현재가정보':[{'현재가':'-500.5','미결제약정':'200000','시장베이시스':'-0.3'}],
                '선옵시간대별체결':[{'코스피200':'500.8'}]}
        snapshot=futures_snapshot(result,'F1','2026-10-15T16:30:00+09:00')
        self.assertEqual(snapshot['values']['fut_price'],500.5)
        self.assertEqual(snapshot['values']['k200'],500.8)
        self.assertIsNone(snapshot['market_asof'])

    def test_blank_response_is_not_zero_price(self):
        with self.assertRaises(ValueError):futures_snapshot({'선옵현재가정보':[{'현재가':''}]},'F1','2026-10-15T16:30:00+09:00')

    def test_snapshot_does_not_create_live_measurements_or_signals(self):
        at=stamp('2026-10-15T16:30:00+09:00')
        snapshot=futures_snapshot({'선옵현재가정보':[{'현재가':'500'}]},'F1',at.isoformat())
        with tempfile.TemporaryDirectory() as directory:
            engine=Engine(config(),directory)
            self.assertTrue(engine.ingest({'market_snapshot':snapshot},at))
            state=engine.compute(at)
            self.assertEqual(state['market_snapshots']['futures']['values']['fut_price'],500)
            self.assertIsNone(engine.val('fut_price',at))
            self.assertEqual(state['verdict']['side'],0)
            engine.store.close()

    def test_monthly_option_rows_keep_actual_iv_and_delta(self):
        snapshot=options_snapshot({'풋종목결제월별시세':[{'종목코드':'P1','행사가':'500.00','미결제약정':'123','내재변동성':'22.5','델타':'-0.4'}]},'P','202611','2026-10-15T16:30:00+09:00')
        self.assertEqual(snapshot['rows'][0]['delta_raw'],-0.4)
        self.assertEqual(snapshot['rows'][0]['oi'],123)
        self.assertEqual(snapshot['month'],'202611')
