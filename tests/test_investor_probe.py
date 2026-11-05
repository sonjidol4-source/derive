import json
import tempfile
import unittest
from pathlib import Path
from investor_probe import inspect_response,save_report,RECORD


class InvestorProbeTests(unittest.TestCase):
    def inspect(self,result):
        return inspect_response(result,'A016C000','foreign','외국인','2026-10-15T18:30:00+09:00')

    def test_exact_future_row_preserves_signed_daily_quantity(self):
        report=self.inspect({RECORD:[{'종목코드':'A016C000','투자자별순매수수량':'-1234'}]})
        self.assertTrue(report['future_quantity_found'])
        self.assertEqual(report['daily_net_contracts'],-1234)
        self.assertEqual(report['scope'],'daily_net_trades')

    def test_options_quantity_cannot_become_futures_quantity(self):
        report=self.inspect({RECORD:[{'종목코드':'B016B860','투자자별순매수수량':'9000'}]})
        self.assertFalse(report['future_quantity_found'])
        self.assertIsNone(report['daily_net_contracts'])

    def test_missing_response_is_unknown_and_zero_is_valid(self):
        self.assertIsNone(self.inspect(None)['daily_net_contracts'])
        self.assertEqual(self.inspect({RECORD:[{'종목코드':'A016C000','투자자별순매수수량':'0'}]})['daily_net_contracts'],0)

    def test_duplicate_future_rows_require_review(self):
        row={'종목코드':'A016C000','투자자별순매수수량':'1'}
        self.assertIsNone(self.inspect({RECORD:[row,row]})['daily_net_contracts'])

    def test_report_does_not_claim_a_verified_live_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path=save_report([self.inspect(None)],directory)
            report=json.loads(Path(path).read_text(encoding='utf-8'))
            self.assertFalse(report['verified_live_connection'])
            self.assertEqual(report['requests_limit'],2)
